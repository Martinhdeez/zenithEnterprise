/**
 * Taking an answer somewhere else.
 *
 * The property worth protecting is that the sources travel with it. This product's argument
 * is that a generated answer is traceable to a page; prose pasted into a document without
 * its citations is indistinguishable from any other chatbot output, and quietly discards
 * the one thing that makes it worth trusting.
 */

import { describe, expect, it } from "vitest";

import { transcript } from "./transcript";
import type { QueryResult } from "../stream/stream";

const cite = (marker: number, filename: string, page: number) => ({
  marker,
  chunk_id: `chunk-${marker}`,
  document_id: `doc-${marker}`,
  filename,
  page_num: page,
  text: "",
  bboxes: [],
});

const result = (citations: QueryResult["citations"]): QueryResult =>
  ({
    citations,
    consulted: [],
    abstained: false,
    degraded: false,
    reason: null,
    model: "gemini",
    took_retrieval_ms: 1,
    took_generation_ms: 1,
  }) as unknown as QueryResult;

describe("copying an answer", () => {
  it("carries the sources under the prose", () => {
    const copied = transcript("Severance is 20 days per year [1].", result([
      cite(1, "handbook.pdf", 4),
    ]));

    expect(copied).toContain("Severance is 20 days per year [1].");
    expect(copied).toContain("[1] handbook.pdf — page 4");
  });

  it("leaves the markers exactly as the model wrote them", () => {
    // They point at the list underneath. Rewritten into links they would paste as links to
    // nothing the moment they left this application.
    const copied = transcript("First [1] and second [2].", result([
      cite(1, "a.pdf", 1),
      cite(2, "b.pdf", 2),
    ]));

    expect(copied).toContain("First [1] and second [2].");
  });

  it("lists each source once however often it is cited", () => {
    // A list that repeats an entry reads as two sources agreeing, which is a stronger claim
    // than one source cited twice.
    const copied = transcript("Both here [1] and here [1].", result([
      cite(1, "handbook.pdf", 4),
      cite(1, "handbook.pdf", 4),
    ]));

    expect(copied.match(/handbook\.pdf/g)).toHaveLength(1);
  });

  it("orders the sources by marker rather than by arrival", () => {
    const copied = transcript("See [1] and [2].", result([
      cite(2, "second.pdf", 9),
      cite(1, "first.pdf", 3),
    ]));

    expect(copied.indexOf("first.pdf")).toBeLessThan(copied.indexOf("second.pdf"));
  });

  it("omits the heading entirely when nothing was cited", () => {
    // An abstention, or a turn answered from the conversation. "Sources" followed by
    // nothing suggests something was lost on the way to the clipboard.
    const copied = transcript("I could not find that in your documents.", result([]));

    expect(copied).not.toContain("Sources");
    expect(copied).toBe("I could not find that in your documents.");
  });
});
