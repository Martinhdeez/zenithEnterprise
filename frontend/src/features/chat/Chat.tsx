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
import { ArrowUp, MessageSquare, Square } from "lucide-react";

import { history, type HistoryEntry } from "@/features/history";
import { listDocuments, type DocumentSummary } from "@/features/documents";
import { complete, mentionAt, mentioned, type Mention } from "./compose/mentions";
import { MentionMenu } from "./compose/MentionMenu";
import { asThread, reduce, type AnswerState } from "./answer/answerState";
import { streamQuery, type Citation } from "./stream/stream";
import { Answer } from "./answer/Answer";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useT } from "@/shared/i18n/useT";

const INITIAL: AnswerState = { phase: "idle" };

/** Long enough that typing a word is one request, short enough to feel immediate — the same
    value the command palette settled on, for the same reason. */
const MENTION_DEBOUNCE_MS = 200;

/** How many documents the mention menu offers at once. More than fits without scrolling is
    a list to read rather than a menu to pick from; the answer to "mine isn't here" is to
    type another letter, which is cheaper than scrolling. */
const MENTION_LIMIT = 6;

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
 * knowledge.
 *
 * **Both are demonstrated rather than described.** This screen used to make them in four
 * bullet points with icons — every fact carries a citation, it abstains rather than
 * inventing, it remembers the thread, use Search for passages. All true, and all read as
 * documentation: somebody arriving here met a manual before they met a tool, and every
 * claim in it was one they could have verified by asking a single question.
 *
 * So the claims are shown by the record instead. Each past question carries what it
 * actually produced — the passages it cited, or that it found nothing — which makes the
 * citation promise and the abstention promise visible as facts about this corpus rather
 * than as assurances about the software. The one sentence that survives is the one nothing
 * on screen can demonstrate on its own: that answers come from the corpus and never from
 * what the model happens to know.
 *
 * Past questions come from `GET /query/history` — the real record, which is what this
 * endpoint holds. `Search`'s equivalent list is local storage precisely because searches
 * are *not* written there; here the data is the right data.
 */
function EmptyState({ token, onAsk }: { token: string; onAsk: (question: string) => void }) {
  const t = useT();
  // The whole entry, not just its text: what a question *produced* is the part that shows
  // the product working, and it is already in the response.
  const [recent, setRecent] = useState<HistoryEntry[]>([]);

  useEffect(() => {
    let cancelled = false;
    void history(token)
      .then((page) => {
        if (cancelled) return;
        // Deduplicated by question: asking the same thing twice should not fill the list
        // with it, and the first occurrence is the most recent.
        const seen = new Set<string>();
        setRecent(
          page.entries
            .filter((entry) => !seen.has(entry.question) && seen.add(entry.question))
            .slice(0, 4),
        );
      })
      .catch(() => !cancelled && setRecent([]));
    return () => {
      cancelled = true;
    };
  }, [token]);

  return (
    <div className="mx-auto flex max-w-xl flex-col items-center gap-6 py-12 text-center">
      <div className="flex size-12 items-center justify-center rounded-full border border-input bg-secondary">
        <MessageSquare className="size-5 text-primary" />
      </div>

      <div className="space-y-1.5">
        <h2 className="text-2xl font-normal text-foreground">{t("Ask your documents")}</h2>
        <p className="text-sm text-muted-foreground">
          {t("Answered only from what is in your corpus — never from what the model happens to know.")}
        </p>
      </div>

      {recent.length > 0 ? (
        <div className="w-full space-y-2">
          <p className="text-xs tracking-wide text-muted-foreground/70 uppercase">{t("Asked here")}</p>
          <ul className="flex flex-col gap-3">
            {recent.map((entry) => (
              <li key={entry.query_id}>
                <button
                  type="button"
                  onClick={() => onAsk(entry.question)}
                  className="flex w-full items-center justify-between gap-4 rounded-2xl border border-input bg-secondary/50 px-4 py-3 text-left transition-colors hover:border-primary/50 hover:bg-secondary"
                >
                  <span className="truncate text-sm text-foreground">{entry.question}</span>
                  {/* What it produced, in the apparatus face — a count is a measurement and
                      reads as one. This is the citation promise and the abstention promise
                      shown as facts about this corpus rather than asserted about the
                      software. */}
                  <span className="shrink-0 font-mono text-xs text-muted-foreground">
                    {entry.citations > 0
                      ? t("{count} cited", { count: entry.citations })
                      : t("found nothing")}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <div className="w-full space-y-2">
          {/* Nothing has been asked yet, so there is nothing to show and the honest thing
              is to offer a way in rather than manufacture evidence. */}
          <p className="text-xs tracking-wide text-muted-foreground/70 uppercase">{t("Try")}</p>
          <div className="flex flex-col gap-3">
            {STARTERS.map((question) => (
              <button
                key={question}
                type="button"
                onClick={() => onAsk(question)}
                className="truncate rounded-2xl border border-input bg-secondary/50 px-4 py-3 text-left text-sm text-muted-foreground transition-colors hover:border-primary/50 hover:bg-secondary hover:text-foreground"
              >
                {question}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
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
  const t = useT();
  const [state, dispatch] = useReducer(reduce, INITIAL);
  // Every turn before the live one — pushed the moment a *new* question starts, not when
  // the old one finishes, so a cancelled or errored turn still keeps its place in the
  // thread instead of vanishing from the scrollback.
  const [turns, setTurns] = useState<AnswerState[]>([]);
  const [question, setQuestion] = useState("");
  const inflight = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // The mention being typed, the documents offered for it, and the row the keyboard is on.
  const [mention, setMention] = useState<Mention | null>(null);
  const [matches, setMatches] = useState<DocumentSummary[]>([]);
  const [active, setActive] = useState(0);
  // Every document mentioned so far this session. Kept rather than replaced by each search
  // so that `mentioned()` — which resolves the ids from the text — can still find a file
  // that was named three edits ago and no longer matches what is in the box.
  const [known, setKnown] = useState<DocumentSummary[]>([]);

  // Searched on the server, like the palette. Filtering one page in the browser finds the
  // documents near the top of the list and silently misses the rest, which reads as the
  // document not existing.
  useEffect(() => {
    if (!mention) return;
    const timer = setTimeout(() => {
      void listDocuments(token, null, null, mention.query.trim() || undefined)
        .then((page) => {
          setMatches(page.items.slice(0, MENTION_LIMIT));
          setActive(0);
          setKnown((current) => {
            const seen = new Set(current.map((document) => document.id));
            return [...current, ...page.items.filter((document) => !seen.has(document.id))];
          });
        })
        .catch(() => setMatches([]));
    }, MENTION_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [token, mention]);

  // The scope, derived from the text on every render rather than held in its own state.
  // Deleting `@handbook.pdf` from the box has to un-scope the question, and a separate list
  // of chips would have to be kept in step with the words by hand — which is the bug where
  // an answer is quietly restricted to a document the user cannot see mentioned anywhere.
  const scope = mentioned(question, known);

  const pick = useCallback(
    (document: DocumentSummary) => {
      setQuestion((current) => {
        const at = mentionAt(current, inputRef.current?.selectionStart ?? current.length);
        return at ? complete(current, at, document.filename) : current;
      });
      setMention(null);
      inputRef.current?.focus();
    },
    [],
  );

  const ask = useCallback(
    async (asked: string) => {
      inflight.current?.abort();
      const controller = new AbortController();
      inflight.current = controller;

      const thread = [...turns, state];
      setTurns((current) => (state.phase === "idle" ? current : [...current, state]));
      dispatch({ type: "ask", question: asked });
      try {
        await streamQuery(
          asked,
          token,
          {
            onToken: (text) => dispatch({ type: "token", text }),
            onResult: (result) => {
              dispatch({ type: "result", result });
              // The first citation opens on its own, so the source is beside the answer
              // without anybody having to be told to click it. That is the product's own
              // argument — every claim checkable against its page — making itself.
              //
              // Nothing opens on an abstention: it has no citations, and leaving the
              // previous document on screen next to "no answer was found in your
              // documents" would be the interface contradicting the sentence beside it.
              const first = result.citations[0];
              if (first) onCitation(first);
            },
            onError: (message) => dispatch({ type: "error", message }),
          },
          {
            labels,
            signal: controller.signal,
            history: asThread(thread),
            documents: scope.map((document) => document.id),
          },
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
    // `state` and `turns` deliberately included: the closure needs the *current* thread at
    // the moment a new question starts — to carry the live turn into `turns`, and to send
    // the finished ones as the context the next answer is allowed to refer to. Omitting
    // `turns` would send a thread frozen at the first question.
    // `scope` too: it is derived from the composer's text, and an `ask` closed over a
    // stale one would send the previous turn's mentions with this turn's question.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [token, labels, state, turns, question, onCitation],
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
        {mention && (
          <MentionMenu
            documents={matches}
            active={active}
            onPick={pick}
            onHover={setActive}
          />
        )}

        {/* What the question is restricted to, in the same words the composer uses. The
            answer will be grounded in these documents and nothing else, and a scope the
            user cannot see is a scope they cannot correct — this is the only place that
            says so before they press send. */}
        {scope.length > 0 && (
          <p className="mb-2 flex flex-wrap items-center gap-1.5 px-1 text-xs text-muted-foreground">
            <span>{t("Answering from")}</span>
            {scope.map((document) => (
              <span
                key={document.id}
                className="max-w-[16rem] truncate rounded-md border border-primary/40 bg-primary/15 px-1.5 py-0.5 text-foreground"
              >
                {document.filename}
              </span>
            ))}
            <span>{t("only")}</span>
          </p>
        )}

        {/* One rounded pill rather than an input-plus-button row — the border lives on
            this wrapper and the input itself is borderless inside it, which is the
            difference between "a text field next to a button" and the single composer
            every chat interface this is modelled on uses. One surface too: the wrapper
            takes the field's own colour so the two do not read as stacked shapes. */}
        {/* `rounded-full`, matching `SearchField` exactly rather than approximately. It was
            `rounded-3xl`, which is derived from `--radius` — so when the radius dropped to
            0.375rem this went from about 22px to about 13px and started reading as a
            rectangle beside a pill. The two places a person types in this product should be
            the same shape, and "nearly the same" is the version that breaks the first time
            a token moves. */}
        <div className="flex items-center gap-2 rounded-full border border-input bg-input/30 py-1.5 pr-1.5 pl-4 shadow-sm transition-colors focus-within:border-primary/40">
          <Input
            ref={inputRef}
            value={question}
            onChange={(event) => {
              setQuestion(event.target.value);
              setMention(mentionAt(event.target.value, event.target.selectionStart ?? 0));
            }}
            // The cursor can move without the text changing — an arrow key, a click into
            // the middle of a word — and the menu has to follow it, or it stays open over
            // a mention the caret has already left.
            onSelect={(event) => {
              const field = event.target as HTMLInputElement;
              setMention(mentionAt(field.value, field.selectionStart ?? 0));
            }}
            onKeyDown={(event) => {
              if (!mention) return;
              if (event.key === "Escape") {
                event.preventDefault();
                setMention(null);
              } else if (event.key === "ArrowDown") {
                event.preventDefault();
                setActive((current) => (current + 1) % Math.max(matches.length, 1));
              } else if (event.key === "ArrowUp") {
                event.preventDefault();
                setActive(
                  (current) => (current - 1 + matches.length) % Math.max(matches.length, 1),
                );
              } else if ((event.key === "Enter" || event.key === "Tab") && matches[active]) {
                // Enter accepts the highlighted document rather than sending the question.
                // Sending a half-typed mention is the one outcome nobody wants: the
                // question goes off unscoped with `@han` sitting in the middle of it.
                event.preventDefault();
                pick(matches[active]);
              }
            }}
            onBlur={() => setMention(null)}
            placeholder={t("Ask a question — @ to answer from one document")}
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
              aria-label={t("Stop generating")}
              className="size-9 shrink-0 rounded-full bg-foreground text-background hover:bg-foreground/90"
            >
              <Square className="size-3 fill-current" />
            </Button>
          ) : (
            <Button
              type="submit"
              size="icon"
              disabled={!question.trim()}
              aria-label={t("Send")}
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
