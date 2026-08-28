/**
 * The conversation about one document, in the panel where the results were.
 *
 * Not a third column. The product is two panels — what you are reading, and what you are
 * doing about it — and a conversation about the open document belongs in the second, not
 * squeezed in beside it. The results are not destroyed to make room: they stay mounted
 * behind the breadcrumb in the header above, which is what makes leaving cheap enough that
 * anchoring is not a trap.
 *
 * **Stage 1 renders the shell and nothing else.** No request is issued here yet: the
 * automatic first turn arrives in stage 2 and the composer in stage 3. It is on the page
 * this early on purpose — design is blocked until the markup is real, and a shell with the
 * right structure is what unblocks it. The alternative, styling markup that is still
 * moving, is the thing this ordering exists to prevent.
 */

import type { T } from "@/shared/i18n/useT";

export interface Anchor {
  /** What the conversation is scoped to. Every turn retrieves inside this and nowhere else. */
  documentId: string;
  /** Shown, so the reader can see what the answers are bounded by. */
  filename: string;
  /** The search that led here. It becomes the first turn rather than being re-typed. */
  question: string;
}

export function AnchoredChat({ anchor, t }: { anchor: Anchor; t: T }) {
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

      <p className="text-sm text-muted-foreground">{t("Preparing the answer…")}</p>
    </section>
  );
}
