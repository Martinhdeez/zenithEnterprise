/**
 * The batch, and the two mistakes it is built to avoid.
 *
 * Uploading two hundred files one at a time, each waiting for the *server* to finish
 * ingesting before the next begins, is an overnight job — ingestion is minutes per document
 * and the browser gains nothing by watching. And opening two hundred parallel uploads takes
 * the API's connection pool away from every other user of the installation.
 */

import { describe, expect, it, vi } from "vitest";

import {
  CONCURRENCY,
  cancellable,
  enqueue,
  pooled,
  summarise,
  update,
  type QueueItem,
} from "./uploadQueue";

const file = (name: string, size = 100) => new File(["x".repeat(size)], name);

const item = (id: string, phase: QueueItem["phase"]): QueueItem => ({
  id,
  file: file(id),
  phase,
  percent: 0,
});

describe("building the queue", () => {
  it("gives every file a row", () => {
    expect(enqueue([file("a.pdf"), file("b.pdf")]).map((row) => row.file.name)).toEqual([
      "a.pdf",
      "b.pdf",
    ]);
  });

  it("starts everything queued", () => {
    expect(enqueue([file("a.pdf")])[0]?.phase).toBe("queued");
  });

  it("keeps two copies of the same file apart", () => {
    // Dropping the same file twice is something people do, and two rows sharing an id
    // would update each other — one would show the other's progress.
    const [first, second] = enqueue([file("same.pdf"), file("same.pdf")]);

    expect(first?.id).not.toBe(second?.id);
  });
});

describe("updating one row", () => {
  it("touches only that row", () => {
    const rows = [item("a", "queued"), item("b", "queued")];

    const next = update(rows, "a", { phase: "done" });

    expect(next[0]?.phase).toBe("done");
    expect(next[1]?.phase).toBe("queued");
  });

  it("does not mutate what it was given", () => {
    const rows = [item("a", "queued")];

    update(rows, "a", { phase: "done" });

    expect(rows[0]?.phase).toBe("queued");
  });
});

describe("the summary", () => {
  it("counts by file rather than by byte", () => {
    // "14 of 200" is a number somebody can act on. A byte percentage moves in jumps that
    // do not match what the rows are doing.
    const summary = summarise([
      item("a", "done"),
      item("b", "done"),
      item("c", "uploading"),
      item("d", "queued"),
    ]);

    expect(summary).toMatchObject({ total: 4, done: 2, active: 2, percent: 50 });
  });

  it("counts a failure as settled, not as still running", () => {
    // A batch with one permanent failure must still be able to finish. Counting it as
    // active leaves the screen saying "1 in progress" forever.
    const summary = summarise([item("a", "done"), item("b", "error")]);

    expect(summary).toMatchObject({ failed: 1, active: 0, finished: true, percent: 100 });
  });

  it("is not finished before anything has been added", () => {
    expect(summarise([]).finished).toBe(false);
  });
});

describe("running the batch", () => {
  it("never exceeds the limit", async () => {
    // The bound is the point: the API pool is ten connections for the whole installation,
    // so an unbounded batch is a client taking it away from everybody else.
    let running = 0;
    let peak = 0;

    await pooled(Array.from({ length: 20 }, (_, i) => i), 3, async () => {
      running += 1;
      peak = Math.max(peak, running);
      await new Promise((resolve) => setTimeout(resolve, 1));
      running -= 1;
    });

    expect(peak).toBe(3);
  });

  it("processes every item", async () => {
    const seen: number[] = [];

    await pooled([1, 2, 3, 4, 5], 2, async (n) => {
      seen.push(n);
    });

    expect(seen.sort()).toEqual([1, 2, 3, 4, 5]);
  });

  it("lets a free worker take the next item rather than waiting for its group", async () => {
    // Fixed slices would hold two finished uploads behind one slow one in the same group.
    // Workers pull from a shared cursor, so a worker that finishes early moves on.
    const order: string[] = [];
    const durations = [30, 1, 1, 1];

    await pooled([0, 1, 2, 3], 2, async (index) => {
      await new Promise((resolve) => setTimeout(resolve, durations[index]));
      order.push(String(index));
    });

    // Item 0 is slow; 2 and 3 are taken by the worker that finished 1, so 0 lands last.
    expect(order[order.length - 1]).toBe("0");
  });

  it("does not spawn more workers than there are items", async () => {
    const work = vi.fn(async () => {});

    await pooled([1], CONCURRENCY, work);

    expect(work).toHaveBeenCalledTimes(1);
  });

  it("keeps going after one item throws", async () => {
    // A duplicate in a batch of two hundred must not abandon the other hundred and
    // ninety-nine. The caller catches per item; this asserts the pool does not unwind.
    const done: number[] = [];

    await pooled([1, 2, 3], 2, async (n) => {
      try {
        if (n === 2) throw new Error("that one is a duplicate");
        done.push(n);
      } catch {
        // handled per item, exactly as Upload.tsx does
      }
    });

    expect(done.sort()).toEqual([1, 3]);
  });
});

describe("cancelling", () => {
  it("can stop a file that has not started and one that is uploading", () => {
    expect(cancellable("queued")).toBe(true);
    expect(cancellable("uploading")).toBe(true);
  });

  it("cannot stop a file the server is already ingesting", () => {
    // `POST /documents` has answered, the row exists and the worker holds the job. A
    // cancel button here would claim the work stops when nothing in the browser can stop
    // it; undoing one of these means deleting the document.
    expect(cancellable("processing")).toBe(false);
  });

  it("offers nothing to stop on a settled row", () => {
    expect(cancellable("done")).toBe(false);
    expect(cancellable("error")).toBe(false);
    expect(cancellable("cancelled")).toBe(false);
  });

  it("counts a cancellation apart from a failure", () => {
    // The whole reason `cancelled` is its own phase. Someone who stopped two files on
    // purpose has not had two files fail, and the line above the list is what they read
    // to decide whether the migration went well.
    const summary = summarise([
      item("a", "done"),
      item("b", "cancelled"),
      item("c", "cancelled"),
      item("d", "error"),
    ]);

    expect(summary.cancelled).toBe(2);
    expect(summary.failed).toBe(1);
    expect(summary.done).toBe(1);
  });

  it("treats a cancelled row as settled so the batch can finish", () => {
    // Otherwise a cancelled batch sits at "2 in progress" for ever, over rows nobody is
    // waiting for, and the progress bar never reaches the end.
    const summary = summarise([item("a", "done"), item("b", "cancelled")]);

    expect(summary.active).toBe(0);
    expect(summary.finished).toBe(true);
    expect(summary.percent).toBe(100);
  });
});
