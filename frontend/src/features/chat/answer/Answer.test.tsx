/**
 * Rendering an answer, and the two rules that are not styling.
 *
 * A citation marker must only become clickable once the result has arrived, and an
 * abstained result must replace the streamed prose on screen — not merely be announced
 * above it.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { AnswerState } from "./answerState";
import type { Citation, QueryResult } from "../stream/stream";
import { Answer } from "./Answer";

const citation: Citation = {
  marker: 1,
  chunk_id: "c1",
  document_id: "d1",
  filename: "gdpr.pdf",
  page_num: 33,
  text: "The controller shall notify…",
  bboxes: [{ x0: 0.1, y0: 0.2, x1: 0.9, y1: 0.25 }],
};

const result = (over: Partial<QueryResult> = {}): QueryResult => ({
  query_id: "q1",
  answer: "Within 72 hours [1].",
  citations: [citation],
  abstained: false,
  consulted: [{ document_id: "d1", filename: "gdpr.pdf", page_num: 33 }],
  model: "llama3.1:8b",
  degraded: false,
  reason: null,
  took_retrieval_ms: 1200,
  took_generation_ms: 9000,
  ...over,
});

const final = (over: Partial<QueryResult> = {}): AnswerState => ({
  phase: "final",
  question: "q",
  result: result(over),
});

describe("<Answer />", () => {
  it("makes a citation marker clickable once the result arrives", () => {
    const onCitation = vi.fn();
    render(<Answer state={final()} onCitation={onCitation} />);

    fireEvent.click(screen.getByRole("button", { name: "[1]" }));

    expect(onCitation).toHaveBeenCalledWith(citation);
  });

  it("leaves markers as plain text while still streaming", () => {
    // Not a dead button. The citations arrive with the result, so until then there is
    // nothing behind the marker and rendering it as clickable would be a lie.
    const onCitation = vi.fn();

    render(
      <Answer
        state={{ phase: "streaming", question: "q", text: "Within 72 hours [1]." }}
        onCitation={onCitation}
      />,
    );

    expect(screen.queryByRole("button", { name: "[1]" })).toBeNull();
    expect(screen.getByText(/72 hours/)).toBeTruthy();
  });

  it("names what is happening before any token arrives", () => {
    // F11 measured 1.2 s to first token, and 4.5 s during an upload. That gap is always
    // there, and an empty bubble in it reads as a hang.
    render(
      <Answer state={{ phase: "retrieving", question: "q" }} onCitation={vi.fn()} />,
    );

    expect(screen.getByText(/searching your documents/i)).toBeTruthy();
  });

  it("shows an abstention and the documents it consulted", () => {
    // mvp.md 2.10: "I found nothing" and "I looked at nothing" are different statements,
    // and only one of them is about the corpus.
    render(
      <Answer
        state={final({
          abstained: true,
          answer: "The documents provided do not contain an answer to this question.",
          citations: [],
        })}
        onCitation={vi.fn()}
      />,
    );

    expect(screen.getByText(/no answer was found/i)).toBeTruthy();
    expect(screen.getByText(/gdpr\.pdf/)).toBeTruthy();
    // And *only* the banner. The sentence the model was handed is a contract the backend
    // matches on, not prose for a reader: printing it says the same thing twice, and says
    // the second one in English however the question was asked.
    expect(screen.queryByText(/documents provided do not contain/i)).toBeNull();
  });

  it("surfaces a degraded answer rather than swallowing it", () => {
    // F11 found a configuration where the reranker failed on every request for as long as
    // nobody looked. The UI is the last place that can make that visible.
    //
    // The reason arrives as a finished sentence from `retrieval/degradation.py` and is shown
    // as it stands. It used to be prefixed with "Answer quality reduced", which said the
    // frightening half twice and the useful half once — and it is the frightening half that
    // makes somebody discard an answer that was correct.
    const reason =
      "Advanced result ordering is temporarily unavailable — the same documents were found, " +
      "but their order is less refined than usual.";
    render(<Answer state={final({ degraded: true, reason })} onCitation={vi.fn()} />);

    expect(screen.getByText(reason)).toBeTruthy();
  });

  it("renders an error without pretending it was an answer", () => {
    render(
      <Answer
        state={{ phase: "error", question: "q", message: "No model configured." }}
        onCitation={vi.fn()}
      />,
    );

    expect(screen.getByRole("alert").textContent).toBe("No model configured.");
  });
});
