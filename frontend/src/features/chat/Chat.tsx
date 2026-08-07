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
import { ArrowUp, MessageSquare, Quote, Search as SearchIcon, ShieldCheck, Square } from "lucide-react";

import { history } from "@/features/history";
import { reduce, type AnswerState } from "./answerState";
import { streamQuery, type Citation } from "./stream";
import { Answer } from "./Answer";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const INITIAL: AnswerState = { phase: "idle" };

/** Shapes of question this corpus can answer, for somebody who has never used it. Kept
    generic — the tenant's documents are not known here — and only shown until there is
    real history to offer in their place. */
const STARTERS = [
  "What are the main obligations described here?",
  "Summarise the key points on penalties.",
  "What deadlines are mentioned?",
];

/**
 * What the screen says before anyone has asked anything.
 *
 * It used to be a heading and one line, which left the two things nobody guesses
 * unexplained: that every sentence carries a citation you can click to open the page it
 * came from, and that the model is required to refuse rather than fill a gap from its own
 * knowledge. Both are the point of the product, and the empty screen is the only moment
 * there is room to say them.
 *
 * Past questions come from `GET /query/history` — the real record, which is what this
 * endpoint holds. `Search`'s equivalent list is local storage precisely because searches
 * are *not* written there; here the data is the right data.
 */
function EmptyState({ token, onAsk }: { token: string; onAsk: (question: string) => void }) {
  const [recent, setRecent] = useState<string[]>([]);

  useEffect(() => {
    let cancelled = false;
    void history(token)
      .then((page) => {
        if (cancelled) return;
        // Deduplicated: asking the same thing twice should not fill the list with it.
        const asked = page.entries.map((entry) => entry.question);
        setRecent([...new Set(asked)].slice(0, 3));
      })
      .catch(() => !cancelled && setRecent([]));
    return () => {
      cancelled = true;
    };
  }, [token]);

  const offered = recent.length > 0 ? recent : STARTERS;

  return (
    <div className="mx-auto flex max-w-xl flex-col items-center gap-6 py-12 text-center">
      <div className="flex size-12 items-center justify-center rounded-full border border-input bg-secondary">
        <MessageSquare className="size-5 text-primary" />
      </div>

      <div className="space-y-1.5">
        <p className="text-lg font-medium text-foreground">Ask your documents</p>
        <p className="text-sm text-muted-foreground">
          Answered only from what is in your corpus — never from what the model happens to
          know.
        </p>
      </div>

      <ul className="w-full space-y-2.5 text-left">
        <Point icon={<Quote className="size-4" />}>
          Every fact is followed by a citation. Click one to open the page it came from,
          highlighted.
        </Point>
        <Point icon={<ShieldCheck className="size-4" />}>
          If your documents do not answer the question, it says so instead of inventing an
          answer.
        </Point>
        <Point icon={<SearchIcon className="size-4" />}>
          Looking for the passages themselves rather than a written answer? Use Search.
        </Point>
      </ul>

      <div className="w-full space-y-2">
        <p className="text-xs tracking-wide text-muted-foreground/70 uppercase">
          {recent.length > 0 ? "Ask again" : "Try"}
        </p>
        <div className="flex flex-col gap-1.5">
          {offered.map((question) => (
            <button
              key={question}
              type="button"
              onClick={() => onAsk(question)}
              className="truncate rounded-lg border border-input bg-card px-3.5 py-2 text-left text-sm text-muted-foreground transition-colors hover:border-primary/50 hover:text-foreground"
            >
              {question}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function Point({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <li className="flex items-start gap-2.5 text-sm text-muted-foreground">
      <span className="mt-0.5 shrink-0 text-primary">{icon}</span>
      <span>{children}</span>
    </li>
  );
}

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
    // Asked, not typed. Putting the text in the composer left it sitting there after the
    // answer had streamed, so the next question had to start with deleting the last one —
    // and the question is already on screen at the top of its own turn, which is where a
    // thread shows what was asked. The composer's job is what you are about to send.
    setQuestion("");
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
        {empty && <EmptyState token={token} onAsk={(asked) => void ask(asked)} />}

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
            every chat interface this is modelled on uses. One surface too: the wrapper
            takes the field's own colour so the two do not read as stacked shapes. */}
        <div className="flex items-center gap-2 rounded-3xl border border-input bg-input/30 py-1.5 pr-1.5 pl-4 shadow-sm transition-colors focus-within:border-primary/40">
          <Input
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="Ask a question about your documents"
            aria-label="Question"
            maxLength={1000}
            // `dark:bg-transparent` is load-bearing, same as the search bar: the base
            // `Input` carries `dark:bg-input/30`, which outlives a plain `bg-transparent`
            // in the dark theme and painted the field a shade off the pill around it.
            className="h-8 flex-1 border-0 bg-transparent p-0 text-foreground shadow-none placeholder:text-muted-foreground focus-visible:ring-0 dark:bg-transparent"
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
