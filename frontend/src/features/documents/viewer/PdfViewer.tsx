/**
 * The citation viewer: the page, and the box the answer came from.
 *
 * mvp.md 2.9 set the bar — a citation is not the string "page 34"; clicking it opens the
 * PDF **on that page, with the chunk highlighted**. Every layer since F5 has carried
 * normalised bounding boxes for this component, and this is the first time they are used.
 *
 * Only the cited page is rendered, not the document. A 400-page PDF rendered in full costs
 * seconds and memory to show one paragraph, and the user arrived here by clicking a
 * specific citation — they asked for one page. Neighbouring pages are one click away, which
 * is the case worth optimising for rather than the one worth pre-loading.
 */

import { useEffect, useRef, useState } from "react";

import { boxesOnPage, scrollTargetFor, toRect, type Box } from "./highlight";
import type { Citation } from "@/features/chat";

// The worker is loaded from the bundle rather than a CDN. This product is installed inside
// networks with no egress, and a viewer that silently fails to render because a script host
// was unreachable would be indistinguishable from a broken document.
import * as pdfjs from "pdfjs-dist";
import PdfWorker from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { useT } from "@/shared/i18n/useT";

pdfjs.GlobalWorkerOptions.workerSrc = PdfWorker;

interface Props {
  citation: Citation | null;
  token: string;
}

export function PdfViewer({ citation, token }: Props) {
  const t = useT();
  const canvas = useRef<HTMLCanvasElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);

  /**
   * The page this citation was found on.
   *
   * `Citation.page_num` is nullable because a text document has none, and this component
   * never receives one — `App` routes by the document's media type. The type cannot say
   * that, so the fallback is here rather than a non-null assertion: an assertion that is
   * ever wrong crashes the viewer, and 1 renders the document's first page, which is the
   * only sensible thing to show if the routing above ever changes.
   */
  const cited = citation?.page_num ?? 1;

  useEffect(() => {
    if (citation) setPage(cited);
  }, [citation, cited]);

  /**
   * Bring the highlight into view once the page has rendered.
   *
   * The panel is a third of the screen and the page is drawn at its natural width, so a
   * passage in the lower half of a page is below the fold — and the reader has to hunt for
   * the very thing they clicked to see. `scrollTargetFor` was written for this, with a
   * careful note about not putting the highlight flush against the top edge, and was called
   * by nothing: the product's central interaction stopped one step short of finishing.
   *
   * Depends on `size`, which is set when the canvas has been painted. Scrolling before that
   * moves a container whose content has no height yet.
   */
  useEffect(() => {
    const container = scroller.current;
    const sheet = canvas.current;
    if (!citation || !size || !container || !sheet) return;

    const boxes = boxesOnPage(citation.bboxes as unknown as Box[], page);
    if (!boxes.length) return;

    const rect = toRect(boxes[0]!, size.width, size.height);
    container.scrollTop = scrollTargetFor(rect, sheet.offsetTop, container.clientHeight);
  }, [citation, page, size]);

  useEffect(() => {
    if (!citation) return;
    let cancelled = false;

    void (async () => {
      setError(null);
      try {
        const response = await fetch(`/documents/${citation.document_id}/file`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!response.ok) {
          // F4 made deletion physical and cascading, so a citation can outlive its
          // document — in a query history, or in a tab left open. Saying so beats a
          // viewer that renders nothing and explains nothing.
          setError(
            response.status === 404
              ? "That document is no longer available."
              : "The document could not be opened.",
          );
          return;
        }

        const document_ = await pdfjs.getDocument({ data: await response.arrayBuffer() }).promise;
        if (cancelled) return;

        const rendered = await document_.getPage(Math.min(page, document_.numPages));
        const viewport = rendered.getViewport({ scale: 1.4 });
        const target = canvas.current;
        if (!target || cancelled) return;

        target.width = viewport.width;
        target.height = viewport.height;
        const context = target.getContext("2d");
        if (!context) return;

        await rendered.render({ canvasContext: context, viewport }).promise;
        if (!cancelled) setSize({ width: viewport.width, height: viewport.height });
      } catch {
        if (!cancelled) setError("The document could not be rendered.");
      }
    })();

    return () => {
      // Rendering is async and a user clicking two citations quickly starts two of them.
      // Without this the slower one wins and paints the wrong page over the right one.
      cancelled = true;
    };
  }, [citation, page, token]);

  if (!citation) {
    return (
      <aside className="flex h-full items-center justify-center p-6 text-center text-sm text-slate-500">{t("Click a citation in an answer to open the page it came from.")}</aside>
    );
  }

  return (
    <aside className="flex h-full flex-col">
      <header className="border-b border-slate-200 px-4 py-3">
        <p className="truncate font-mono text-[13px] font-medium">{citation.filename}</p>
        <p className="text-sm text-slate-500">
          Page {page}
          {page !== cited && " (cited page " + cited + ")"}
        </p>
      </header>

      <div ref={scroller} className="flex-1 overflow-auto bg-slate-100 p-4">
        {error ? (
          <p role="alert" className="text-sm text-red-800">
            {error}
          </p>
        ) : (
          <div className="relative mx-auto w-fit shadow-sm">
            <canvas ref={canvas} data-testid="pdf-canvas" />
            {size &&
              boxesOnPage(citation.bboxes as unknown as Box[], page).map((box, index) => {
                const rect = toRect(box, size.width, size.height);
                return (
                  <div
                    key={index}
                    data-testid="citation-highlight"
                    aria-hidden
                    // Multiply-blended amber rather than a solid fill: the text under the
                    // highlight has to stay readable, because reading it is the entire
                    // reason the user clicked.
                    className="pointer-events-none absolute bg-amber-300/40 mix-blend-multiply ring-1 ring-amber-500"
                    style={{
                      left: rect.left,
                      top: rect.top,
                      width: rect.width,
                      height: rect.height,
                    }}
                  />
                );
              })}
          </div>
        )}
      </div>

      <footer className="flex items-center justify-between border-t border-slate-200 px-4 py-2 text-sm">
        <button
          type="button"
          onClick={() => setPage((current) => Math.max(1, current - 1))}
          disabled={page <= 1}
          className="rounded-sm px-2 py-1 disabled:opacity-40"
        >{t("← Previous")}</button>
        <button
          type="button"
          onClick={() => setPage(cited)}
          className="rounded-sm px-2 py-1 text-sky-700"
        >{t("Back to citation")}</button>
        <button
          type="button"
          onClick={() => setPage((current) => current + 1)}
          className="rounded-sm px-2 py-1"
        >{t("Next →")}</button>
      </footer>
    </aside>
  );
}
