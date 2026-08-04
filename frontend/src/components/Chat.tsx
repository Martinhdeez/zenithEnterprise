/**
 * The ask box and the stream it drives.
 *
 * `useReducer` over the state machine in `api/answer.ts` rather than a store: the server
 * owns everything worth persisting, and what this component holds is one in-flight request
 * and its accumulated tokens.
 */

import { useCallback, useReducer, useRef, useState } from "react";

import { reduce, type AnswerState } from "../api/answer";
import { streamQuery, type Citation } from "../api/stream";
import { Answer } from "./Answer";

const INITIAL: AnswerState = { phase: "idle" };

interface Props {
  token: string;
  onCitation: (citation: Citation) => void;
  /** From `GET /tenant/status`. An ask box over an empty corpus can only disappoint. */
  searchable: boolean;
}

export function Chat({ token, onCitation, searchable }: Props) {
  const [state, dispatch] = useReducer(reduce, INITIAL);
  const [question, setQuestion] = useState("");
  const inflight = useRef<AbortController | null>(null);

  const ask = useCallback(
    async (asked: string) => {
      inflight.current?.abort();
      const controller = new AbortController();
      inflight.current = controller;

      dispatch({ type: "ask", question: asked });
      try {
        await streamQuery(
          asked,
          token,
          {
            onToken: (text) => dispatch({ type: "token", text }),
            onResult: (result) => dispatch({ type: "result", result }),
            onError: (message) => dispatch({ type: "error", message }),
          },
          { signal: controller.signal },
        );
      } catch (error) {
        // An abort is the user asking something else, not a failure to report.
        if (controller.signal.aborted) return;
        dispatch({
          type: "error",
          message: error instanceof Error ? error.message : "The request failed.",
        });
      }
    },
    [token],
  );

  if (!searchable) {
    // Better than a search box that can only ever return nothing.
    return (
      <p className="rounded-md border border-slate-200 bg-slate-50 p-4 text-sm text-slate-600">
        There are no documents to search yet. Upload one to get started.
      </p>
    );
  }

  return (
    <section className="space-y-4">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          const asked = question.trim();
          if (asked) void ask(asked);
        }}
        className="flex gap-2"
      >
        <input
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="Ask a question about your documents"
          aria-label="Question"
          maxLength={1000}
          className="flex-1 rounded-md border border-slate-300 px-3 py-2"
        />
        <button
          type="submit"
          className="rounded-md bg-slate-900 px-4 py-2 text-white disabled:opacity-50"
          disabled={!question.trim()}
        >
          Ask
        </button>
      </form>

      {state.phase !== "idle" && (
        <article className="rounded-md border border-slate-200 p-4">
          <p className="mb-3 text-sm font-medium text-slate-500">{state.question}</p>
          <Answer state={state} onCitation={onCitation} />
        </article>
      )}
    </section>
  );
}
