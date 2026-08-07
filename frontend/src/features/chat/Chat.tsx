/**
 * The ask box and the stream it drives.
 *
 * `useReducer` over the state machine in `api/answer.ts` rather than a store: the server
 * owns everything worth persisting, and what this component holds is one in-flight request
 * and its accumulated tokens.
 *
 * Laid out like the chat interfaces this is deliberately modelled on: a message thread that
 * scrolls in its own region, an input pinned to the bottom of the panel rather than sitting
 * above the answer. `turns` is this session's thread — every prior question this component
 * has asked, kept purely so there is something to scroll back through; `@/features/history` already
 * persists questions server-side (`GET /query/history`) for the record that outlives a tab,
 * and this array duplicates none of that, it just gives the *current* conversation a shape
 * instead of replacing itself on every question the way earlier versions of this screen did.
 */

import { useCallback, useEffect, useLayoutEffect, useReducer, useRef, useState } from "react";
import { ArrowUp, Square } from "lucide-react";

import { reduce, type AnswerState } from "./answerState";
import { streamQuery, type Citation } from "./stream";
import { Answer } from "./Answer";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const INITIAL: AnswerState = { phase: "idle" };

interface Props {
  token: string;
  onCitation: (citation: Citation) => void;
  /** From `GET /tenant/status`. An ask box over an empty corpus can only disappoint. */
  searchable: boolean;
  /**
   * Narrow the search to a folder the user selected.
   *
   * Passed straight through to the API, which refuses a label the caller does not hold
   * with a 403 rather than an empty result — so this can only ever narrow, never widen.
   */
  labels?: string[];
  /**
   * Set by History when a past question is clicked. A new object on every click, even for
   * a repeated question, so this effect fires again instead of seeing an unchanged prop.
   */
  prefill?: { text: string; nonce: number } | null;
}

export function Chat({ token, onCitation, searchable, labels, prefill }: Props) {
  const [state, dispatch] = useReducer(reduce, INITIAL);
  // Every turn before the live one — pushed the moment a *new* question starts, not when
  // the old one finishes, so a cancelled or errored turn still keeps its place in the
  // thread instead of vanishing from the scrollback.
  const [turns, setTurns] = useState<AnswerState[]>([]);
  const [question, setQuestion] = useState("");
  const inflight = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const ask = useCallback(
    async (asked: string) => {
      inflight.current?.abort();
      const controller = new AbortController();
      inflight.current = controller;

      setTurns((current) => (state.phase === "idle" ? current : [...current, state]));
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
          { labels, signal: controller.signal },
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
    // `state` deliberately included: the closure needs the *current* live turn at the
    // moment a new question starts, to carry it into `turns` before replacing it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [token, labels, state],
  );

  const cancel = useCallback(() => {
    inflight.current?.abort();
    // The abort alone doesn't move the state machine anywhere — `ask`'s own catch block
    // sees `aborted` and deliberately does nothing, since the normal reason for an abort
    // is a *new* question about to overwrite this state anyway. A manual cancel has no
    // such follow-up, so it has to make that transition itself or the spinner and the
    // progress bar would sit there forever, showing work that stopped.
    dispatch({ type: "reset" });
  }, []);

  useEffect(() => {
    if (!prefill) return;
    setQuestion(prefill.text);
    void ask(prefill.text);
    // `prefill.nonce` is the trigger — a click on the same past question a second time
    // still asks it again, which `[prefill]` alone (identity-equal to itself) would not.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefill?.nonce]);

  // Follows the bottom of the thread as it grows — a new question, a streaming token, a
  // finished turn — the way every chat interface this is modelled on does. Not smooth: a
  // token arriving every few dozen milliseconds would fight a CSS transition for the same
  // scrollTop and produce jitter, not a scroll.
  useLayoutEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [turns, state]);

  if (!searchable) {
    // Better than a search box that can only ever return nothing.
    return (
      <p className="rounded-xl border border-border bg-card p-4 text-sm text-muted-foreground">
        There are no documents to search yet. Upload one to get started.
      </p>
    );
  }

  const busy = state.phase === "retrieving" || state.phase === "streaming";
  const empty = turns.length === 0 && state.phase === "idle";

  return (
    <div className="flex h-full flex-col">
      <div ref={scrollRef} className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-3xl space-y-6 px-6 py-6 2xl:max-w-4xl">
        {empty && (
          <div className="flex h-full flex-col items-center justify-center gap-1.5 text-center">
            <p className="text-lg font-medium text-foreground">Ask your documents</p>
            <p className="text-sm text-muted-foreground">
              Whatever you ask is answered only from what's in your corpus — nothing else.
            </p>
          </div>
        )}

        {[...turns, state]
          .filter((turn) => turn.phase !== "idle")
          .map((turn, index) => (
            <div key={index} className="space-y-2">
              <p className="ml-auto w-fit max-w-[85%] rounded-2xl bg-secondary px-4 py-2 text-sm text-foreground">
                {turn.question}
              </p>
              <div className="max-w-[85%]">
                <Answer state={turn} onCitation={onCitation} />
              </div>
            </div>
          ))}
        </div>
      </div>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          const asked = question.trim();
          if (!asked) return;
          setQuestion("");
          void ask(asked);
        }}
        className="mx-auto w-full max-w-3xl shrink-0 p-4 2xl:max-w-4xl"
      >
        {/* One rounded pill rather than an input-plus-button row — the border lives on
            this wrapper and the input itself is borderless inside it, which is the
            difference between "a text field next to a button" and the single composer
            every chat interface this is modelled on uses. */}
        <div className="flex items-center gap-2 rounded-3xl border border-border bg-card py-1.5 pl-4 pr-1.5 shadow-sm transition-colors focus-within:border-primary/40">
          <Input
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="Ask a question about your documents"
            aria-label="Question"
            maxLength={1000}
            className="h-8 flex-1 border-0 bg-transparent p-0 text-foreground shadow-none placeholder:text-muted-foreground focus-visible:ring-0"
          />
          {busy ? (
            // The stop-generating affordance every one of these interfaces settles on: a
            // filled square, not a second text button — "Cancel" as a word competes with
            // the composer's placeholder for attention exactly when the eye should be on
            // the streaming answer instead.
            <Button
              type="button"
              size="icon"
              onClick={cancel}
              aria-label="Stop generating"
              className="size-9 shrink-0 rounded-full bg-foreground text-background hover:bg-foreground/90"
            >
              <Square className="size-3 fill-current" />
            </Button>
          ) : (
            <Button
              type="submit"
              size="icon"
              disabled={!question.trim()}
              aria-label="Send"
              className="size-9 shrink-0 rounded-full bg-primary text-white transition-opacity hover:bg-primary/90 disabled:opacity-30"
            >
              <ArrowUp className="size-4" />
            </Button>
          )}
        </div>
      </form>
    </div>
  );
}
