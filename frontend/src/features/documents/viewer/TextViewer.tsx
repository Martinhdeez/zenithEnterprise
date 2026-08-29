/**
 * The citation viewer for documents that have no pages.
 *
 * Same promise as `PdfViewer` — click a citation, see the passage in its source — with
 * simpler geometry. A `.txt` or `.md` has exactly one text, the bytes on disk, so the
 * passage is a character range in it and the highlight is a `<mark>` around that range.
 *
 * **This is why offsets work here and not in a PDF.** pdfplumber's extracted text and
 * pdf.js's text layer disagree on whitespace, ligatures and hyphenation, so an offset
 * computed against one lands in the wrong place in the other — which is what put bounding
 * boxes in the schema. A text file has no second rendering to disagree with.
 *
 * The whole file is fetched and shown, not one screenful. `PdfViewer` renders a single page
 * because a 400-page PDF costs seconds and memory; a text document that survived the upload
 * limit is a string, and slicing it into pretend pages would reintroduce exactly the page
 * number this format does not have.
 */

import { useEffect, useRef, useState } from "react";

import type { Citation } from "@/features/chat";
import { useFormat, useT } from "@/shared/i18n/useT";

interface Props {
  citation: Citation;
  token: string;
}

export function TextViewer({ citation, token }: Props) {
  const t = useT();
  const format = useFormat();
  const highlight = useRef<HTMLElement>(null);
  const [body, setBody] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setBody(null);
    setError(null);

    void (async () => {
      try {
        const response = await fetch(`/documents/${citation.document_id}/file`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!response.ok) throw new Error(String(response.status));
        const text = await response.text();
        if (!cancelled) setBody(text);
      } catch {
        if (!cancelled) setError("The document could not be opened.");
      }
    })();

    return () => {
      // Two citations clicked quickly start two fetches; without this the slower one wins
      // and shows the wrong document. Same hazard `PdfViewer` guards against.
      cancelled = true;
    };
  }, [citation.document_id, token]);

  /**
   * Bring the highlight into view once the text is on screen.
   *
   * A passage a thousand lines down is below the fold, and the reader would have to hunt
   * for the very thing they clicked. `block: "center"` rather than `"start"`: a highlight
   * flush against the top edge reads as the beginning of the document rather than as a
   * place within it, which is the same note `scrollTargetFor` makes for the PDF.
   */
  useEffect(() => {
    if (body === null) return;
    // `scrollIntoView` is not implemented in jsdom; a viewer must not fail to render
    // because the scroll it asked for was unavailable.
    highlight.current?.scrollIntoView?.({ block: "center" });
  }, [body, citation.chunk_id]);

  if (error) {
    return (
      <aside className="flex h-full flex-col">
        <header className="border-b border-border px-4 py-3">
          <p className="truncate font-mono text-[13px] font-medium text-foreground">{citation.filename}</p>
        </header>
        <p role="alert" className="p-4 text-sm text-destructive">
          {error}
        </p>
      </aside>
    );
  }

  // Clamped rather than trusted. The offsets come from the server and are correct, but a
  // document re-uploaded with different bytes under the same id would slice out of range —
  // and `String.slice` answers that silently with an empty string, which would render as a
  // citation pointing at nothing.
  const start = body === null ? 0 : Math.max(0, Math.min(citation.char_start, body.length));
  const end = body === null ? 0 : Math.max(start, Math.min(citation.char_end, body.length));

  return (
    <aside className="flex h-full flex-col">
      <header className="border-b border-border px-4 py-3">
        <p className="truncate font-mono text-[13px] font-medium text-foreground">{citation.filename}</p>
        {/* Deliberately not "page 1". This document has no pages, and naming a position
            that does not exist is the thing the nullable column was introduced to stop. */}
        <p className="text-sm text-muted-foreground">
          {body === null
            ? t("Opening…")
            : t("Characters {from}–{to}", { from: format.number(start), to: format.number(end) })}
        </p>
      </header>

      <div className="flex-1 overflow-auto bg-secondary/30 p-4">
        {body === null ? (
          <p className="text-sm text-muted-foreground">{t("Opening…")}</p>
        ) : (
          /* `pre-wrap` keeps the author's line breaks and indentation — a runbook's shape
             is part of its meaning — while still wrapping long lines into the panel. */
          <pre
            data-testid="text-body"
            className="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-foreground"
          >
            {body.slice(0, start)}
            <mark
              ref={highlight}
              data-testid="citation-highlight"
              // The same amber as the PDF highlight, for the same reason: the text under
              // it has to stay readable, because reading it is why the user clicked.
              className="bg-amber-300/40 text-foreground ring-1 ring-amber-500"
            >
              {body.slice(start, end)}
            </mark>
            {body.slice(end)}
          </pre>
        )}
      </div>
    </aside>
  );
}
