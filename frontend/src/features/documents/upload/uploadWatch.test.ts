/**
 * The three ways a watch can end, and why they must stay three.
 *
 * Before this module existed, all of them returned a document and the caller wrote
 * `status === "failed" ? error : done`. So a document still `parsing` when the two-minute
 * watch expired was reported to the user as finished — and the documents that take longest to
 * ingest are exactly the ones that hit the limit. The batch summary said the migration was
 * complete while the server was still working through it.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import { phaseFor, untilSettled, type Watched } from "./uploadWatch";
import type { DocumentSummary } from "../api";

vi.mock("../api", () => ({ getDocument: vi.fn() }));

const { getDocument } = await import("../api");
const polls = vi.mocked(getDocument);

const doc = (status: string, detail: string | null = null): DocumentSummary => ({
  id: "d1",
  filename: "report.pdf",
  description: null,
  sha256: "abc",
      media_type: "application/pdf",
  status,
  status_detail: detail,
  page_count: null,
  size_bytes: 10,
  uploaded_by: null,
  created_at: "2026-08-25T00:00:00Z",
  label_ids: [],
});

// Every watch in this file runs with a zero-length sleep and a tiny limit. The real values
// are 1.5 s and 80 attempts — two minutes of wall clock that no test should spend.
const fast = { pollMs: 0, limit: 4 };

beforeEach(() => polls.mockReset());

describe("watching until the server settles", () => {
  it("reports a document that reaches ready", async () => {
    polls.mockResolvedValueOnce(doc("ready"));
    const watched = await untilSettled("t", doc("pending"), () => {}, fast);
    expect(watched).toMatchObject({ outcome: "settled", document: { status: "ready" } });
  });

  it("reports a document that reaches failed", async () => {
    polls.mockResolvedValueOnce(doc("failed", "no extractable text layer"));
    const watched = await untilSettled("t", doc("parsing"), () => {}, fast);
    expect(watched.outcome).toBe("settled");
    expect(watched.document.status_detail).toBe("no extractable text layer");
  });

  it("does not poll at all when the server is already finished", async () => {
    const watched = await untilSettled("t", doc("ready"), () => {}, fast);
    expect(polls).not.toHaveBeenCalled();
    expect(watched.outcome).toBe("settled");
  });

  it("reports the stages it passed through", async () => {
    const seen: string[] = [];
    polls.mockResolvedValueOnce(doc("chunking")).mockResolvedValueOnce(doc("ready"));
    await untilSettled("t", doc("parsing"), (status) => seen.push(status), fast);
    expect(seen).toEqual(["parsing", "chunking"]);
  });
});

describe("the two endings that are not an answer", () => {
  it("times out rather than claiming the document is done", async () => {
    polls.mockResolvedValue(doc("embedding"));
    const watched = await untilSettled("t", doc("pending"), () => {}, fast);
    expect(watched).toMatchObject({ outcome: "timeout", document: { status: "embedding" } });
  });

  it("keeps the last status actually seen when a poll cannot be sent", async () => {
    polls.mockResolvedValueOnce(doc("chunking")).mockRejectedValueOnce(new Error("offline"));
    const watched = await untilSettled("t", doc("pending"), () => {}, fast);
    expect(watched).toMatchObject({ outcome: "unreachable", document: { status: "chunking" } });
  });

  it("accepts a document that finishes on the very last attempt", async () => {
    // The loop sleeps and *then* polls, so the final poll's result arrives after the counter
    // is spent. Discarding it would report a finished document as timed out.
    polls
      .mockResolvedValueOnce(doc("parsing"))
      .mockResolvedValueOnce(doc("chunking"))
      .mockResolvedValueOnce(doc("embedding"))
      .mockResolvedValueOnce(doc("ready"));
    const watched = await untilSettled("t", doc("pending"), () => {}, fast);
    expect(watched.outcome).toBe("settled");
  });
});

describe("what the row is told", () => {
  const phase = (outcome: Watched["outcome"], status: string, detail: string | null = null) =>
    phaseFor({ outcome, document: doc(status, detail) });

  it("marks only ready as done", () => {
    expect(phase("settled", "ready").phase).toBe("done");
  });

  it("shows the server's own reason for a failure", () => {
    expect(phase("settled", "failed", "password protected")).toEqual({
      phase: "error",
      message: "password protected",
    });
  });

  it("does not call an unrecognised terminal status a success", () => {
    // The defect this module was extracted for, in its general form: "anything that is not
    // failed" reports every future status as finished work.
    expect(phase("settled", "quarantined").phase).toBe("error");
  });

  it("sends a timed-out row to Documents rather than calling it done", () => {
    const result = phase("timeout", "embedding");
    expect(result.phase).toBe("unresolved");
    expect(result.message).toMatch(/Documents/);
  });

  it("says the status is unavailable when polling failed", () => {
    const result = phase("unreachable", "parsing");
    expect(result.phase).toBe("unresolved");
    expect(result.message).toMatch(/unavailable/);
  });
});
