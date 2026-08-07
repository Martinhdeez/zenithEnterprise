/**
 * The transitions that decide whether the fabrication gate survives the client.
 *
 * These are the assertions worth having: the backend can strip every invented marker in
 * flight and still be defeated by a component that appends the result to the tokens instead
 * of replacing them.
 */

import { describe, expect, it } from "vitest";

import { displayed, isProvisional, reduce, type AnswerState } from "./answerState";
import type { QueryResult } from "./stream";

const result = (over: Partial<QueryResult> = {}): QueryResult => ({
  query_id: "q1",
  answer: "The controller must notify within 72 hours [1].",
  citations: [],
  abstained: false,
  consulted: [],
  model: "llama3.1:8b",
  degraded: false,
  reason: null,
  took_retrieval_ms: 1200,
  took_generation_ms: 9000,
  ...over,
});

function run(events: Parameters<typeof reduce>[1][]): AnswerState {
  return events.reduce(reduce, { phase: "idle" } as AnswerState);
}

describe("the answer state machine", () => {
  it("acknowledges retrieval before any token arrives", () => {
    // F11: the first token cannot arrive before ~1.2 s, and ~4.5 s during an upload. A UI
    // that shows nothing in that window reads as a hang.
    const state = run([{ type: "ask", question: "when must a breach be reported?" }]);

    expect(state.phase).toBe("retrieving");
    expect(isProvisional(state)).toBe(true);
  });

  it("accumulates tokens in order", () => {
    const state = run([
      { type: "ask", question: "q" },
      { type: "token", text: "The controller " },
      { type: "token", text: "must notify." },
    ]);

    expect(displayed(state)).toBe("The controller must notify.");
  });

  it("replaces the streamed text with the authoritative answer", () => {
    // Not appends. The result is the answer; the tokens were a preview of it.
    const state = run([
      { type: "ask", question: "q" },
      { type: "token", text: "partial text that was still being written" },
      { type: "result", result: result() },
    ]);

    expect(displayed(state)).toBe("The controller must notify within 72 hours [1].");
    expect(displayed(state)).not.toContain("partial");
  });

  it("discards streamed text entirely when the answer is withdrawn", () => {
    // The assertion this file exists for. `abstained` means the server discarded the
    // answer because it cited nothing valid — the prose was already sent, and leaving it
    // on screen under a notice would ship exactly the fabrication the gate prevents.
    const abstention = "The documents provided do not contain an answer to this question.";

    const state = run([
      { type: "ask", question: "q" },
      { type: "token", text: "The penalty is 4% of global turnover" },
      { type: "result", result: result({ abstained: true, answer: abstention }) },
    ]);

    expect(displayed(state)).toBe(abstention);
    expect(displayed(state)).not.toContain("4%");
    expect(isProvisional(state)).toBe(false);
  });

  it("stops being provisional once the result lands", () => {
    // Drives whether citation markers are clickable: a marker in streamed text has no
    // citation object behind it, because the citations arrive with the result.
    const state = run([
      { type: "ask", question: "q" },
      { type: "token", text: "text [1]" },
      { type: "result", result: result() },
    ]);

    expect(isProvisional(state)).toBe(false);
  });

  it("ignores tokens that arrive after the result", () => {
    // Defensive rather than expected. If it ever happened, appending them would put
    // unvalidated text back on screen after the authoritative answer had replaced it.
    const state = run([
      { type: "ask", question: "q" },
      { type: "result", result: result() },
      { type: "token", text: " and some extra" },
    ]);

    expect(displayed(state)).toBe("The controller must notify within 72 hours [1].");
  });

  it("keeps the question visible when a request fails", () => {
    const state = run([
      { type: "ask", question: "what is the rate?" },
      { type: "error", message: "The language model could not be reached." },
    ]);

    expect(state.phase).toBe("error");
    expect(state).toHaveProperty("question", "what is the rate?");
  });
});
