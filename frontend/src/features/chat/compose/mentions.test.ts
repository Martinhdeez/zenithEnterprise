/**
 * When the mention menu opens, and — the half that is easy to forget — when it does not.
 *
 * The scope of an answer is decided by what these functions return. A mention that fails to
 * parse silently sends an unscoped question, which returns a *plausible* answer from the
 * whole corpus: the user asked about one document and was answered from all of them, and
 * nothing on screen says so.
 */

import { describe, expect, it } from "vitest";

import { complete, mentionAt, mentioned } from "./mentions";

describe("finding the mention being typed", () => {
  it("opens on a bare @", () => {
    expect(mentionAt("@", 1)).toEqual({ start: 0, end: 1, query: "" });
  });

  it("carries what has been typed after it", () => {
    expect(mentionAt("what does @hand", 15)?.query).toBe("hand");
  });

  it("keeps spaces, because filenames have them", () => {
    // The alternative — stop at the first space — makes every multi-word document
    // unreachable by exactly the mechanism that exists to reach it.
    expect(mentionAt("see @annual report", 18)?.query).toBe("annual report");
  });

  it("ignores an @ inside a word, so an email address is not a mention", () => {
    expect(mentionAt("write to sam@example.com", 24)).toBeNull();
  });

  it("ignores an @ on an earlier line", () => {
    expect(mentionAt("@handbook\nand now what?", 22)).toBeNull();
  });

  it("reads the mention the cursor is in, not the last one in the text", () => {
    // The caret sits just after "@a"; the later "@b" is ahead of it and is somebody else's
    // mention.
    expect(mentionAt("@a and @b", 2)?.query).toBe("a");
  });

  it("gives up once the query is too long to be a filename fragment", () => {
    expect(mentionAt(`@${"x".repeat(61)}`, 62)).toBeNull();
  });

  it("stays closed when there is no @ at all", () => {
    expect(mentionAt("what is the severance policy", 28)).toBeNull();
  });
});

describe("completing a mention", () => {
  it("puts the filename in, not the id", () => {
    // A UUID in the composer is a string the user cannot read and cannot correct.
    const text = complete("what does @hand", { start: 10, end: 15, query: "hand" }, "handbook.pdf");

    expect(text).toBe("what does @handbook.pdf ");
  });

  it("keeps whatever followed the cursor", () => {
    const text = complete("@han say?", { start: 0, end: 4, query: "han" }, "handbook.pdf");

    expect(text).toBe("@handbook.pdf  say?");
  });

  it("leaves a trailing space so the next word is not swallowed by the mention", () => {
    const text = complete("@h", { start: 0, end: 2, query: "h" }, "handbook.pdf");

    expect(text.endsWith(" ")).toBe(true);
  });
});

describe("resolving the scope from the text", () => {
  const documents = [
    { id: "doc-a", filename: "handbook.pdf" },
    { id: "doc-b", filename: "policy.pdf" },
  ];

  it("finds a mentioned document", () => {
    expect(mentioned("@handbook.pdf what is severance?", documents)).toEqual([documents[0]]);
  });

  it("finds several", () => {
    expect(mentioned("@handbook.pdf and @policy.pdf", documents)).toHaveLength(2);
  });

  it("drops a mention that has been deleted", () => {
    // The property that makes the text the single source of truth. Held as separate state,
    // this is the bug where an answer stays restricted to a document the user removed and
    // can no longer see mentioned anywhere.
    expect(mentioned("what is severance?", documents)).toEqual([]);
  });

  it("does not match a filename that is merely written out without an @", () => {
    expect(mentioned("what does handbook.pdf say?", documents)).toEqual([]);
  });
});
