/**
 * The conversation about one document, in the panel where the results were.
 *
 * Not a third column. The product is two panels — what you are reading, and what you are
 * doing about it — and a conversation about the open document belongs in the second, not
 * squeezed in beside it. The results are not destroyed to make room: they stay mounted
 * behind the breadcrumb in the header above, which is what makes leaving cheap enough that
 * anchoring is not a trap.
 *
 * **Everything that renders or validates an answer is borrowed, not rewritten.** `reduce`,
 * `AnswerState` and `Answer` already carry the retrieving/streaming/final progression and
 * the rule that accumulated tokens are provisional while only `result.answer` is
 * authoritative. A second implementation here would give the citation guarantee a second
 * owner, which is the one thing in this product that must have exactly one.
 *
 * What is genuinely new is the *anchor*: one document id on every request, first turn and
 * follow-ups alike, and a first turn asked on the user's behalf.
 *
 * **On the transcript container.** The specification called for shadcn's `MessageScroller`,
 * and it is not here. Installing it pulls `@shadcn/react` and rewrites `components/ui/button.tsx`
 * — a file this codebase has customised, `size="icon-sm"` among other things, and which is
 * used by every screen. Trading a scroll behaviour for an unreviewed rewrite of the most
 * shared component in the tree is not a trade worth making silently, so the thread scrolls
 * the way `Chat` already scrolls and `MessageScroller` stays on the table as its own change,
 * where its diff can be read.
 */

import { useCallback, useEffect, useLayoutEffect, useReducer, useRef, useState } from "react";
import { CornerDownLeft } from "lucide-react";

import { Answer } from "../answer/Answer";
import { asThread, reduce, type AnswerState } from "../answer/answerState";
import { streamQuery, type Citation } from "../stream/stream";
import { Button } from "@/components/ui/button";
import { useT } from "@/shared/i18n/useT";

export interface Anchor {
  /** What the conversation is scoped to. Every turn retrieves inside this and nowhere else. */
  documentId: string;
  /** Shown, so the reader can see what the answers are bounded by. */
  filename: string;
  /** The search that led here. It becomes the first turn rather than being re-typed. */
  question: string;
}

export function AnchoredChat({
  anchor,
  token,
  onCitation,
}: {
  anchor: Anchor;
  token: string;
  onCitation: (citation: Citation) => void;
}) {
  const t = useT();
  const [settled, setSettled] = useState<AnswerState[]>([]);
  const [current, dispatch] = useReducer(reduce, { phase: "idle" } as AnswerState);
  const [draft, setDraft] = useState("");
  const scroller = useRef<HTMLDivElement | null>(null);

  /**
   * One request per anchor, and the ref is what enforces it.
   *
   * `useEffect` runs twice in development under StrictMode, and a second run here is not a
   * wasted render — it is a second generation, billed, against a provider the customer pays
   * for. The dependency array alone does not prevent that; remembering what was already
   * asked does.
   */
  const asked = useRef<string | null>(null);

  const ask = useCallback(
    (question: string, history: { question: string; answer: string }[]) => {
      const controller = new AbortController();
      dispatch({ type: "ask", question });
      void streamQuery(
        question,
        token,
        {
          onToken: (text) => dispatch({ type: "token", text }),
          onResult: (result) => dispatch({ type: "result", result }),
          onError: (message) => dispatch({ type: "error", message }),
        },
        {
          // The anchor, on every turn and not only the first. A follow-up that dropped it
          // would widen to the whole corpus while the line above still promised one file.
          documents: [anchor.documentId],
          // Bounded by `conversation.py` on the server — six turns, answers truncated,
          // questions not. Sent whole; the server decides what fits.
          history,
          signal: controller.signal,
        },
      );
      return () => controller.abort();
    },
    [anchor.documentId, token],
  );

  useEffect(() => {
    if (asked.current === anchor.documentId) return;
    asked.current = anchor.documentId;
    return ask(anchor.question, []);
  }, [anchor.documentId, anchor.question, ask]);

  // Set directly rather than `scrollIntoView`, for the reason `Chat` records: a token
  // arriving every few dozen milliseconds would fight a CSS transition for the same
  // `scrollTop` and produce jitter instead of a scroll. Following the newest turn is only
  // right while it is the one at the bottom, which it always is here — this thread has no
  // history to load above it.
  useLayoutEffect(() => {
    const node = scroller.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [settled, current]);

  const send = () => {
    const question = draft.trim();
    if (!question || current.phase === "retrieving" || current.phase === "streaming") return;
    // The finished turn joins the transcript before the new one starts, so the thread the
    // server is sent matches the thread on screen.
    setSettled((previous) => (current.phase === "idle" ? previous : [...previous, current]));
    setDraft("");
    ask(question, asThread(current.phase === "idle" ? settled : [...settled, current]));
  };

  const busy = current.phase === "retrieving" || current.phase === "streaming";

  return (
    <section
      // Named for a screen reader, because the panel changed underneath somebody who
      // pressed a button in a different panel. Without this the region announces as
      // nothing and the change is silent.
      aria-label={t("Conversation about {filename}", { filename: anchor.filename })}
      className="flex min-h-0 flex-col gap-4"
    >
      {/* What the thread is bounded by, said once at the top rather than repeated on every
          turn. A reader who cannot see the scope cannot tell an abstention ("not in this
          document") from an absence ("not in the corpus"), and those are different facts. */}
      <p className="text-sm text-muted-foreground">
        {t("Answering from {filename} only.", { filename: anchor.filename })}
      </p>

      <div ref={scroller} className="flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto">
        {settled.map((turn, index) => (
          <Answer key={index} state={turn} onCitation={onCitation} />
        ))}
        <Answer state={current} onCitation={onCitation} />
      </div>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          send();
        }}
        className="flex items-end gap-2"
      >
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          aria-label={t("Ask about this document")}
          placeholder={t("Ask something else about this document")}
          className="flex-1 rounded-md border border-input bg-input/60 px-3 py-2 text-sm outline-none focus:border-primary"
        />
        <Button type="submit" size="icon-sm" disabled={busy || draft.trim() === ""}>
          <CornerDownLeft className="size-4" />
          <span className="sr-only">{t("Send")}</span>
        </Button>
      </form>
    </section>
  );
}
