/**
 * The three states an answer can be in, and the transition that must not be got wrong.
 *
 * Kept out of the component deliberately. Whether a streamed answer is replaced or appended
 * to is the difference between honouring the fabrication gate and defeating it, and that
 * decision should be testable without rendering anything.
 */

import type { QueryResult } from "../stream/stream";

export type AnswerState =
  | { phase: "idle" }
  | { phase: "retrieving"; question: string }
  | { phase: "streaming"; question: string; text: string }
  | { phase: "final"; question: string; result: QueryResult }
  | { phase: "error"; question: string; message: string };

export type AnswerEvent =
  | { type: "ask"; question: string }
  | { type: "token"; text: string }
  | { type: "result"; result: QueryResult }
  | { type: "error"; message: string }
  | { type: "reset" };

export function reduce(state: AnswerState, event: AnswerEvent): AnswerState {
  switch (event.type) {
    case "ask":
      // `retrieving` rather than jumping straight to `streaming`, because F11 measured
      // that the first token cannot arrive before ~1.2 s of retrieval — and ~4.5 s while
      // someone is uploading. That gap is always there and the UI has to name it rather
      // than show an empty bubble.
      return { phase: "retrieving", question: event.question };

    case "token": {
      if (state.phase !== "retrieving" && state.phase !== "streaming") return state;
      const previous = state.phase === "streaming" ? state.text : "";
      return { phase: "streaming", question: state.question, text: previous + event.text };
    }

    case "result": {
      if (state.phase === "idle" || state.phase === "error") return state;
      // **Replace, never append.** The result is authoritative: when `abstained` is true
      // the streamed prose was discarded server-side and must disappear here too. A client
      // that leaves it on screen under an abstention notice has shipped the fabrication the
      // backend spent two milestones preventing.
      return { phase: "final", question: state.question, result: event.result };
    }

    case "error":
      return {
        phase: "error",
        question: "question" in state ? state.question : "",
        message: event.message,
      };

    case "reset":
      return { phase: "idle" };
  }
}

/** What the user should actually see, given the state. Never the raw tokens once final. */
export function displayed(state: AnswerState): string {
  if (state.phase === "streaming") return state.text;
  if (state.phase === "final") return state.result.answer;
  return "";
}

/**
 * Whether the answer is still provisional.
 *
 * Drives the "still writing" affordance and, more importantly, whether citations are
 * clickable: a marker in streamed text has no citation object behind it yet, because the
 * citations arrive with the result.
 */
export function isProvisional(state: AnswerState): boolean {
  return state.phase === "retrieving" || state.phase === "streaming";
}

/**
 * The thread as the server wants it: finished exchanges only.
 *
 * A turn still streaming, one that errored, and one the user cancelled are all dropped. The
 * question is on screen either way, but sending half an answer as context invites the model
 * to continue it, and sending an error message as an "answer" would have the model explain
 * an error to somebody who can already read it.
 */
export function asThread(states: AnswerState[]): { question: string; answer: string }[] {
  return states
    .filter((state) => state.phase === "final")
    .map((state) => ({ question: state.question, answer: state.result.answer }));
}
