/**
 * What the upload pool is allowed to wait for.
 *
 * `CONCURRENCY` bounds the number of files whose *bytes* are in flight, because that is what
 * competes for the API's connection pool. It was accidentally bounding something else as
 * well: the worker awaited the ingestion watch too, so a slot stayed occupied for as long as
 * the server took to parse, chunk and embed the document — up to the watch's two-minute
 * limit. A twenty-file batch therefore advanced three files at a time and looked frozen
 * between them, which is the opposite of what bounded concurrency was added for.
 *
 * The watch is mocked here to a promise that never resolves. That is the worst case the old
 * code had to survive and did not: with it, the batch must still upload every file.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../api", () => ({
  uploadDocument: vi.fn(),
  getDocument: vi.fn(),
}));

vi.mock("@/features/labels", () => ({
  labels: vi.fn(async () => []),
  LabelPicker: () => null,
}));

vi.mock("./uploadWatch", () => ({
  untilSettled: vi.fn(),
  phaseFor: vi.fn(() => ({ phase: "done" as const })),
}));

const { Upload } = await import("./Upload");
const { uploadDocument } = await import("../api");
const { untilSettled } = await import("./uploadWatch");
const { WATCHING } = await import("./uploadQueue");

const uploads = vi.mocked(uploadDocument);
const watches = vi.mocked(untilSettled);

const pdf = (name: string) => new File(["%PDF-1.4"], name, { type: "application/pdf" });

const document_ = (id: string) => ({
  id,
  filename: `${id}.pdf`,
  description: null,
  sha256: id,
      media_type: "application/pdf",
  status: "pending",
  status_detail: null,
  page_count: null,
  size_bytes: 8,
  uploaded_by: null,
  created_at: "2026-08-25T00:00:00Z",
  label_ids: [],
});

beforeEach(() => {
  uploads.mockReset();
  watches.mockReset();
  uploads.mockImplementation(async (file) => ({
    document: document_(file.name),
    labels: [],
    deduplicated: false,
  }));
});

/** Drop `count` files on the input and confirm the staging table. */
async function drop(count: number) {
  const files = Array.from({ length: count }, (_, index) => pdf(`f${index}.pdf`));
  const input = screen.getByLabelText("Upload PDFs");
  await act(async () => {
    fireEvent.change(input, { target: { files } });
  });
  const confirm = await screen.findByRole("button", { name: /Confirm & process/ });
  await act(async () => {
    fireEvent.click(confirm);
  });
}

async function sendBatch(count: number) {
  render(<Upload token="t" onUploaded={() => {}} />);
  await drop(count);
}

describe("a batch larger than the concurrency limit", () => {
  it("uploads every file even while no ingestion ever finishes", async () => {
    // Never resolves: every document stays mid-ingestion forever.
    watches.mockImplementation(() => new Promise(() => {}));

    await sendBatch(8);

    // The assertion that fails on the old code. Awaiting the watch inside the pool capped
    // this at CONCURRENCY (3) until the two-minute limit expired on each one.
    await waitFor(() => expect(uploads).toHaveBeenCalledTimes(8), { timeout: 3000 });
  });

  it("bounds the watches independently of the uploads", async () => {
    // Never resolving, so every started watch holds its slot. Detaching the watches from the
    // upload pool fixed the batch advancing three files at a time and left two hundred
    // pollers running at once — roughly 130 status requests a second from one tab, at the
    // moment the server is busiest ingesting what that tab just sent.
    watches.mockImplementation(() => new Promise(() => {}));

    await sendBatch(8);

    // Every file's bytes are up...
    await waitFor(() => expect(uploads).toHaveBeenCalledTimes(8), { timeout: 3000 });
    // ...and only `WATCHING` of them are being polled.
    expect(watches).toHaveBeenCalledTimes(WATCHING);
  });

  it("watches the rest as slots free up", async () => {
    // A document waiting for a slot loses nothing: the server keeps its status and the watch
    // reads it whenever it starts. What would be wrong is never starting.
    const finish: (() => void)[] = [];
    watches.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish.push(() =>
            resolve({ outcome: "settled", document: document_("x") } as never),
          );
        }),
    );

    await sendBatch(8);
    await waitFor(() => expect(watches).toHaveBeenCalledTimes(WATCHING), { timeout: 3000 });

    await act(async () => {
      for (const done of [...finish]) done();
    });

    await waitFor(() => expect(watches).toHaveBeenCalledTimes(8), { timeout: 3000 });
  });

  it("refreshes the library once the bytes are in, without waiting for ingestion", async () => {
    // Folder counts and the document list should be right within seconds of the transfer.
    // Tying them to the watches meant they trailed ingestion by up to two minutes per file.
    watches.mockImplementation(() => new Promise(() => {}));
    const onUploaded = vi.fn();
    render(<Upload token="t" onUploaded={onUploaded} />);
    await drop(4);

    await waitFor(() => expect(onUploaded).toHaveBeenCalled(), { timeout: 3000 });
  });
});

describe("a file the server rejects", () => {
  it("does not stop the rest of the batch", async () => {
    // One duplicate in two hundred must not abandon the other hundred and ninety-nine.
    watches.mockImplementation(() => new Promise(() => {}));
    uploads.mockImplementationOnce(async () => {
      throw new Error("already exists");
    });

    await sendBatch(5);

    await waitFor(() => expect(uploads).toHaveBeenCalledTimes(5), { timeout: 3000 });
  });
});

/**
 * The two numbers the row used to compute and discard.
 *
 * `progress()` has returned a rate and an estimate on every event since it was written, and
 * `formatRate`/`formatEta` formatted them for nobody — the row showed a percentage and
 * dropped the rest. On a large upload the percentage is the least useful of the three: it
 * says how far this file has got and nothing about whether the transfer is still moving.
 */
describe("a file whose bytes are still moving", () => {
  it("shows the rate and the time left beside the percentage", async () => {
    watches.mockImplementation(() => new Promise(() => {}));
    // Hold the upload open so the row stays in `uploading`, and report progress the way the
    // XHR does.
    let report: ((event: { loaded: number; total: number }) => void) | undefined;
    uploads.mockImplementation(
      (_file, _token, _labels, options) =>
        new Promise(() => {
          report = options?.onProgress;
        }),
    );

    // Two files, not one: a single dropped file goes to the review step rather than the
    // staging table, and never reaches the queue this is about.
    render(<Upload token="t" onUploaded={() => {}} />);
    await drop(2);

    await waitFor(() => expect(report).toBeDefined());
    // Past the hundred-millisecond guard in `progress()`, deliberately with the real clock.
    // Before it there is no rate worth reporting — which is the *other* test below — and
    // faking the timer here would test the mock rather than the guard.
    await new Promise((resolve) => setTimeout(resolve, 150));

    // 500 kB of a megabyte. The rate is whatever the clock says; what matters is that a rate
    // and an estimate are rendered at all.
    await act(async () => {
      report?.({ loaded: 500_000, total: 1_000_000 });
    });

    expect(await screen.findByText(/50%/)).toBeTruthy();
    expect(screen.getByText(/left/)).toBeTruthy();
  });

  it("shows the percentage alone before there is a sample worth reporting", async () => {
    // `progress()` returns null for both inside the first tenth of a second: dividing by a
    // near-zero elapsed time claims gigabytes per second and no time remaining.
    watches.mockImplementation(() => new Promise(() => {}));
    let report: ((event: { loaded: number; total: number }) => void) | undefined;
    uploads.mockImplementation(
      (_file, _token, _labels, options) =>
        new Promise(() => {
          report = options?.onProgress;
        }),
    );

    render(<Upload token="t" onUploaded={() => {}} />);
    await drop(2);
    await waitFor(() => expect(report).toBeDefined());

    await act(async () => {
      report?.({ loaded: 0, total: 1_000_000 });
    });

    expect(screen.queryByText(/left/)).toBeNull();
  });
});
