/**
 * The conversation about one document, in the panel where the results were.
 *
 * Not a third column. The product is two panels — what you are reading, and what you are
 * doing about it — and a conversation about the open document belongs in the second, not
 * squeezed in beside it. The results are not destroyed to make room: they stay mounted
 * behind the breadcrumb in the header above, which is what makes leaving cheap enough that
 * anchoring is not a trap.
 *
 * **Everything that renders or validates an answer is borrowed, not rewritten.** `reduce`
 * and `AnswerState` carry the retrieving/streaming/final progression, and `Answer` renders
 * it — including the rule that the accumulated tokens are provisional and only
 * `result.answer` is authoritative. A second implementation of that here would be a second
 * place for the citation guarantee to drift out of, which is the one thing in this product
 * that cannot be allowed to have two owners.
 *
 * What is genuinely new is the *anchor*: one document id on every request, and a first turn
 * that is asked on the user's behalf.
 */

import { useEffect, useReducer, useRef } from "react";

import { Answer } from "../answer/Answer";
import { reduce } from "../answer/answerState";
import { streamQuery, type Citation } from "../stream/stream";
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
  const [state, dispatch] = useReducer(reduce, { phase: "idle" });

  /**
   * One request per anchor, and the ref is what enforces it.
   *
   * `useEffect` runs twice in development under StrictMode, and a second run here is not a
   * wasted render — it is a second generation, billed, against a provider the customer pays
   * for. The dependency array alone does not prevent that; remembering what was already
   * asked does.
   */
  const asked = useRef<string | null>(null);

  useEffect(() => {
    if (asked.current === anchor.documentId) return;
    asked.current = anchor.documentId;

    const controller = new AbortController();
    dispatch({ type: "ask", question: anchor.question });
    void streamQuery(
      anchor.question,
      token,
      {
        onToken: (text) => dispatch({ type: "token", text }),
        onResult: (result) => dispatch({ type: "result", result }),
        onError: (message) => dispatch({ type: "error", message }),
      },
      // The anchor, and the whole point of this screen. Enforced in the retrieval SQL
      // rather than here — the client asking nicely for one document would be a filter a
      // forgotten parameter could drop.
      { documents: [anchor.documentId], signal: controller.signal },
    );

    return () => controller.abort();
  }, [anchor.documentId, anchor.question, token]);

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

      <Answer state={state} onCitation={onCitation} />
    </section>
  );
}
