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

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Crosshair, Minus, Plus } from "lucide-react";

import {
  boxesOnPage,
  centredScrollLeft,
  frameTargetFor,
  scrollTargetFor,
  toRect,
  unionRect,
  zoomForColumn,
  type Box,
} from "./highlight";
import type { Citation } from "@/features/chat";

// The worker is loaded from the bundle rather than a CDN. This product is installed inside
// networks with no egress, and a viewer that silently fails to render because a script host
// was unreachable would be indistinguishable from a broken document.
import * as pdfjs from "pdfjs-dist";
import PdfWorker from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { useT } from "@/shared/i18n/useT";

pdfjs.GlobalWorkerOptions.workerSrc = PdfWorker;

/**
 * The scale the viewer has always drawn at, and zoom 1 by definition.
 *
 * Zoom multiplies it rather than replacing it, so "back to normal" is the number 1 and the
 * page every reader has been looking at until now is still what they get.
 */
const BASE_SCALE = 1.4;

/**
 * The bounds come from what the panel actually does at them, not from round numbers.
 *
 * The preview panel opens at 35% of the window. On a 1440px screen that is ~470px of usable
 * width, and an A4 page at `BASE_SCALE` is 833px wide — so the page already overflows the
 * panel at zoom 1, and 0.5 is the point where a whole page width fits inside it. Below that
 * body text stops being text and becomes grey texture, which defeats the purpose of a
 * viewer whose job is letting someone read the sentence a citation names.
 *
 * The ceiling is a canvas limit, not a taste one. At 4x an A4 page is 3332x4716 = 15.7
 * megapixels, ~63MB of RGBA bitmap, and just under the ~16.7Mpx (4096x4096) ceiling Safari
 * enforces on a single canvas. Past that a canvas does not error — it comes back blank.
 */
const MIN_ZOOM = 0.5;
const MAX_ZOOM = 4;

/**
 * The rungs the buttons step between.
 *
 * A multiplicative step from wherever the wheel happened to stop would never land on 1
 * again: pinch to 1.13x, step by 1.25, and the default is unreachable for ever. Snapping to
 * a fixed ladder means every press walks towards a rung and 1 is one of them, so the reader
 * can always get back to the page they know.
 */
const ZOOM_STOPS = [0.5, 0.67, 0.8, 1, 1.25, 1.5, 2, 2.5, 3, 4];

/**
 * How long the gesture has to stop before the page is redrawn sharp.
 *
 * A wheel or a pinch arrives as a burst of dozens of events; re-rendering on each one would
 * queue dozens of pdf.js renders of a canvas that is up to 16 megapixels. So the gesture
 * only changes a CSS transform — which the compositor does for free, and which carries the
 * highlights with it because they live inside the transformed box — and exactly one render
 * happens when the fingers stop.
 */
const SETTLE_MS = 140;

/** The scroller's own `p-4`, as a number, because the fit has to subtract it. */
const SCROLLER_PADDING = 32;

interface Props {
  citation: Citation | null;
  token: string;
  /**
   * Compose the passage in the panel instead of merely scrolling to it.
   *
   * Set for the open a search performs on its own. The page is zoomed until the highlighted
   * column fills the panel, centred horizontally, and the whole passage — not its first line
   * — is framed vertically. A citation the reader clicked gets none of it: they are reading a
   * screen already, and rearranging it under them would be the interface taking the wheel.
   */
  framed?: boolean;
}

function bounded(zoom: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom));
}

export function PdfViewer({ citation, token, framed = false }: Props) {
  const t = useT();
  const canvas = useRef<HTMLCanvasElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  // Read from the parsed document and kept, where it used to be read for a `Math.min` and
  // discarded — which is why "Next" had no ceiling and would walk past the last page.
  const [pages, setPages] = useState<number | null>(null);
  const [pdf, setPdf] = useState<pdfjs.PDFDocumentProxy | null>(null);

  /**
   * Two numbers, because a zoom gesture has two speeds.
   *
   * `zoom` is what the canvas was last *rendered* at; `preview` is what the reader is
   * currently seeing. Between them sits a ratio, applied as a CSS transform to the box
   * holding the canvas **and the highlights together** — so a pinch in progress is a cheap
   * scale of one composited layer, and the highlight cannot drift from the text under it
   * because there is only one coordinate space for it to drift in.
   *
   * When the gesture settles the two converge and the transform returns to identity. The
   * page's laid-out size is `size * ratio` = `BASE_SCALE * preview` either way, so at the
   * moment the sharp render lands nothing moves: only the blur goes.
   */
  const [zoom, setZoom] = useState(1);
  const [preview, setPreview] = useState(1);
  const previewed = useRef(1);
  const settle = useRef<ReturnType<typeof setTimeout>>(undefined);
  const ratio = preview / zoom;

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
   *
   * `size` now also changes every time a zoom settles, and re-running then would drag the
   * reader back to the citation each time they zoomed somewhere else — the scroll fighting
   * the gesture. So it fires once per citation and page: on arrival, which is the moment it
   * was written for.
   */
  const arrived = useRef<string | null>(null);
  useEffect(() => {
    const container = scroller.current;
    const sheet = canvas.current;
    if (!citation || !size || !container || !sheet) return;

    const key = `${citation.chunk_id}:${page}`;
    if (arrived.current === key) return;

    const boxes = boxesOnPage(citation.bboxes as unknown as Box[], page);
    if (!boxes.length) return;

    arrived.current = key;

    if (!framed) {
      const rect = toRect(boxes[0]!, size.width, size.height);
      container.scrollTop = scrollTargetFor(rect, sheet.offsetTop, container.clientHeight);
      return;
    }

    // The whole passage, and the page centred under it. `unionRect` cannot return null here —
    // `boxes` is non-empty — but the null branch is the type's, not a guess about the data.
    const whole = unionRect(boxes.map((box) => toRect(box, size.width, size.height)));
    if (!whole) return;

    container.scrollTop = frameTargetFor(whole, sheet.offsetTop, container.clientHeight);
    container.scrollLeft = centredScrollLeft(container.scrollWidth, container.clientWidth);
  }, [citation, framed, page, size]);

  /**
   * Zoom until the passage's own column fills the panel.
   *
   * Runs before the placement above rather than with it, because it changes the size the
   * placement is computed from: the fit sets a new zoom, pdf.js redraws, `size` changes, and
   * only then is there a page to centre. Clearing `arrived` is what lets the placement effect
   * run a second time on that new size — without it the reader is framed against the page as
   * it was *before* the zoom, which is the one arrangement that is wrong at both axes.
   *
   * Once per citation, keyed by chunk. Not once per page: stepping to the next page is the
   * reader navigating, and re-fitting under them would undo whatever zoom they had chosen.
   */
  const fitted = useRef<string | null>(null);
  useEffect(() => {
    const container = scroller.current;
    if (!framed || !citation || !size || !container) return;
    if (fitted.current === citation.chunk_id) return;

    const boxes = boxesOnPage(citation.bboxes as unknown as Box[], cited);
    if (!boxes.length) return;

    fitted.current = citation.chunk_id;

    // The widest highlighted line stands in for the column. A short last line would ask for a
    // zoom that puts the rest of the paragraph off both edges.
    const column = Math.max(
      ...boxes.map((box) => toRect(box, size.width, size.height).width),
    );
    const next = bounded(
      zoomForColumn(column, container.clientWidth - SCROLLER_PADDING, zoom),
    );
    // A fit that lands where the page already is would clear `arrived` for a redraw that never
    // comes, and the placement would never run at all.
    if (Math.abs(next - zoom) < 0.01) return;

    arrived.current = null;
    anchor.current = null;
    previewed.current = next;
    setPreview(next);
    setZoom(next);
  }, [cited, citation, framed, size, zoom]);

  /**
   * Open the document. Once per citation — not once per page, and emphatically not once per
   * zoom step, which is what turning the wheel would otherwise have cost: a fresh HTTP fetch
   * and a full re-parse of the whole file for a change of scale.
   */
  useEffect(() => {
    if (!citation) return;
    let cancelled = false;
    setPdf(null);
    // Cleared so the scroll effect above cannot measure the outgoing document's page, and so
    // the previous page's highlights are not drawn over the incoming one.
    setSize(null);

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
              ? t("That document is no longer available.")
              : t("The document could not be opened."),
          );
          return;
        }

        const opened = await pdfjs.getDocument({ data: await response.arrayBuffer() }).promise;
        if (cancelled) return;

        setPages(opened.numPages);
        setPdf(opened);
      } catch {
        if (!cancelled) setError("The document could not be rendered.");
      }
    })();

    return () => {
      // Opening is async and a user clicking two citations quickly starts two of them.
      // Without this the slower one wins and shows the wrong document.
      cancelled = true;
    };
  }, [citation, t, token]);

  /**
   * Draw one page at one scale.
   *
   * `size` is set from the very viewport the canvas was sized from, and the highlights are
   * positioned against `size` — so the overlay is scaled by construction rather than by a
   * second calculation that could disagree with the first. That is the whole safety argument
   * for this feature: a highlight that drifts points a reader at the wrong sentence and
   * gives them no way to know it happened.
   */
  useEffect(() => {
    if (!pdf) return;
    let cancelled = false;
    let task: pdfjs.RenderTask | null = null;

    void (async () => {
      try {
        const rendered = await pdf.getPage(Math.min(page, pdf.numPages));
        const viewport = rendered.getViewport({ scale: BASE_SCALE * zoom });
        const target = canvas.current;
        if (!target || cancelled) return;

        // Floored, because a canvas's width is an integer however precise the viewport is —
        // and `size` has to be what the canvas *became*, not what was asked for. Recording
        // the float would put the overlay's coordinate space a fraction of a pixel out of
        // step with the bitmap it is drawn over, at every zoom, for no reason.
        const width = Math.floor(viewport.width);
        const height = Math.floor(viewport.height);
        target.width = width;
        target.height = height;
        const context = target.getContext("2d");
        if (!context) return;

        task = rendered.render({ canvasContext: context, viewport });
        await task.promise;
        if (!cancelled) setSize({ width, height });
      } catch {
        if (!cancelled) setError("The document could not be rendered.");
      }
    })();

    return () => {
      cancelled = true;
      // pdf.js refuses a second render into a canvas that is still being painted, which is
      // exactly what a zoom settling while the previous one is still drawing would ask for.
      task?.cancel();
    };
  }, [page, pdf, zoom]);

  /**
   * Keep the reader looking at the same place when the scale changes.
   *
   * Without this, zooming in walks the page towards its top-left corner and the passage
   * being read slides out of the panel. The anchor is the pointer for a wheel or a pinch,
   * and the centre of the panel for the buttons, which have no pointer position to speak of.
   *
   * A layout effect, not an ordinary one: the scroll offsets have to be corrected in the
   * same frame the box changes size, or the page visibly jumps and comes back.
   */
  const anchor = useRef<{ x: number; y: number } | null>(null);
  const shown = useRef(1);
  useLayoutEffect(() => {
    const container = scroller.current;
    if (!container) return;
    const growth = preview / shown.current;
    shown.current = preview;
    if (growth === 1) return;

    const at = anchor.current ?? { x: container.clientWidth / 2, y: container.clientHeight / 2 };
    container.scrollLeft = (container.scrollLeft + at.x) * growth - at.x;
    container.scrollTop = (container.scrollTop + at.y) * growth - at.y;
  }, [preview]);

  /**
   * The wheel, and the trackpad pinch.
   *
   * A two-finger pinch reaches the page as a `wheel` event with `ctrlKey` set — there is no
   * separate pinch event on the desktop web — so one handler serves both, with a larger
   * constant for the pinch because a pinch reports far smaller deltas than a wheel notch.
   * Exponential rather than additive, so a notch feels the same size at 0.5x and at 4x.
   *
   * Registered by hand rather than as `onWheel` because React attaches `wheel` **passively**
   * at the root, where `preventDefault` is ignored — and without it the pinch is also the
   * browser's own page zoom, which would scale the entire application underneath the
   * document.
   */
  useEffect(() => {
    const container = scroller.current;
    if (!container) return;

    // An arrow rather than a `function` declaration only because a hoisted declaration can be
    // called before the null check above, so TypeScript will not narrow `container` inside it.
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const box = container.getBoundingClientRect();
      anchor.current = { x: event.clientX - box.left, y: event.clientY - box.top };

      const next = bounded(
        previewed.current * Math.exp(-event.deltaY * (event.ctrlKey ? 0.012 : 0.0018)),
      );
      previewed.current = next;
      setPreview(next);
      clearTimeout(settle.current);
      settle.current = setTimeout(() => setZoom(next), SETTLE_MS);
    };

    container.addEventListener("wheel", onWheel, { passive: false });
    // `citation` alone: the scroller is the same element whether the page or the "no longer
    // available" message is inside it, and it unmounts only when there is no citation at all.
    return () => container.removeEventListener("wheel", onWheel);
  }, [citation]);

  useEffect(() => () => clearTimeout(settle.current), []);

  /**
   * One rung up or down. Both numbers move together, so a button press redraws sharp
   * immediately instead of going blurry first — there is no gesture to wait out.
   */
  function step(direction: 1 | -1) {
    const from = previewed.current;
    const below = ZOOM_STOPS.filter((stop) => stop < from - 0.001);
    const next =
      direction > 0
        ? (ZOOM_STOPS.find((stop) => stop > from + 0.001) ?? MAX_ZOOM)
        : (below[below.length - 1] ?? MIN_ZOOM);

    anchor.current = null;
    previewed.current = next;
    clearTimeout(settle.current);
    setPreview(next);
    setZoom(next);
  }

  /**
   * Drag to pan.
   *
   * Not decoration: the wheel zooms now, so it no longer scrolls, and the page overflows the
   * panel at the default zoom already — an A4 page is 833px wide at `BASE_SCALE` against
   * ~470px of panel. Without this the only way to reach the bottom of a page would be the
   * scrollbars, which is a poor answer on a surface whose whole job is reading.
   */
  const drag = useRef<{ x: number; y: number; left: number; top: number } | null>(null);

  function startPan(event: React.PointerEvent<HTMLDivElement>) {
    const container = scroller.current;
    if (event.button !== 0 || !size || !container) return;
    drag.current = {
      x: event.clientX,
      y: event.clientY,
      left: container.scrollLeft,
      top: container.scrollTop,
    };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }

  function pan(event: React.PointerEvent<HTMLDivElement>) {
    const from = drag.current;
    const container = scroller.current;
    if (!from || !container) return;
    container.scrollLeft = from.left - (event.clientX - from.x);
    container.scrollTop = from.top - (event.clientY - from.y);
  }

  function endPan(event: React.PointerEvent<HTMLDivElement>) {
    if (!drag.current) return;
    drag.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  }

  if (!citation) {
    return (
      <aside className="flex h-full items-center justify-center p-6 text-center text-sm text-muted-foreground">{t("Click a citation in an answer to open the page it came from.")}</aside>
    );
  }

  return (
    <aside className="flex h-full flex-col">
      <header className="border-b border-border px-4 py-3">
        <p className="truncate font-mono text-[13px] font-medium">{citation.filename}</p>
        <p className="text-sm text-muted-foreground">
          {page === cited
            ? t("Page {page}", { page })
            : t("Page {page} — cited page {cited}", { page, cited })}
        </p>
      </header>

      <div
        ref={scroller}
        data-testid="pdf-scroller"
        onPointerDown={startPan}
        onPointerMove={pan}
        onPointerUp={endPan}
        onPointerCancel={endPan}
        className={`flex-1 overflow-auto bg-muted p-4 ${size ? "cursor-grab active:cursor-grabbing" : ""}`}
      >
        {error ? (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        ) : (
          <div
            data-testid="pdf-page"
            className="relative mx-auto shadow-sm"
            style={size ? { width: size.width * ratio, height: size.height * ratio } : undefined}
          >
            {/* The canvas and every highlight sit inside this one box, and a gesture scales
                the box. There is no second scale factor to keep in step, which is the whole
                point: a highlight that drifts from the text it marks is worse than no
                highlight at all, because it points a reader at the wrong sentence and gives
                them no way to know. */}
            <div
              data-testid="pdf-sheet"
              className="relative w-fit origin-top-left"
              style={ratio === 1 ? undefined : { transform: `scale(${ratio})` }}
            >
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
          </div>
        )}
      </div>

      {/* Three plain text buttons in a row, all weighing the same, is what this was — and
          the middle one is the most valuable control in the product: it returns to the
          passage an answer was built from, which is the whole argument for showing a
          document at all. It read as the least important of the three.

          So the two page steps become quiet icon buttons at the edges, "back to the
          citation" takes the accent in the middle, and the position is stated between them.
          "Previous / Next" without saying where you are is navigating a thousand-page file
          blind, and the count was already on the parsed document — it was being read for a
          `Math.min` and thrown away.

          The arrows are icons now rather than `←` and `→` inside the label. They were
          sitting in the translation catalogue as part of the string, which asked a
          translator to carry a glyph that does not translate.

          The zoom pair is filed after a hairline rather than mixed in among the arrows:
          these two act on the *view*, the other three act on the *document*, and one rule
          says that more plainly than spacing does. They take the same quiet icon-button
          shape as the page steps, and dim the same way at their limits, because they are
          the same kind of thing. */}
      <footer className="flex items-center gap-2 border-t border-border px-3 py-2">
        <button
          type="button"
          onClick={() => setPage((current) => Math.max(1, current - 1))}
          disabled={page <= 1}
          title={t("Previous page")}
          aria-label={t("Previous page")}
          className="flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground disabled:pointer-events-none disabled:opacity-35"
        >
          <ChevronLeft className="size-4" />
        </button>

        <div className="flex min-w-0 flex-1 items-center justify-center gap-2">
          {/* Tabular so the digits do not shift as the page changes under the cursor. */}
          <span className="shrink-0 font-mono text-xs tabular-nums text-muted-foreground">
            {pages === null ? page : `${page} / ${pages}`}
          </span>
          {page !== cited && (
            // Drawn only when it has somewhere to go. On the cited page it would be a
            // control that does nothing, which is worse than an absent one.
            <button
              type="button"
              onClick={() => setPage(cited)}
              title={t("Back to citation")}
              className="flex min-w-0 items-center gap-1.5 rounded-full bg-primary/10 px-2.5 py-1 text-xs font-medium text-primary transition-colors hover:bg-primary/18"
            >
              <Crosshair className="size-3.5 shrink-0" />
              <span className="truncate">{t("Back to citation")}</span>
            </button>
          )}
        </div>

        <button
          type="button"
          onClick={() => setPage((current) => (pages === null ? current + 1 : Math.min(pages, current + 1)))}
          disabled={pages !== null && page >= pages}
          title={t("Next page")}
          aria-label={t("Next page")}
          className="flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground disabled:pointer-events-none disabled:opacity-35"
        >
          <ChevronRight className="size-4" />
        </button>

        <span aria-hidden className="h-5 w-px shrink-0 bg-border" />

        <button
          type="button"
          onClick={() => step(-1)}
          disabled={preview <= MIN_ZOOM}
          title={t("Zoom out")}
          aria-label={t("Zoom out")}
          className="flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground disabled:pointer-events-none disabled:opacity-35"
        >
          <Minus className="size-4" />
        </button>

        <button
          type="button"
          onClick={() => step(1)}
          disabled={preview >= MAX_ZOOM}
          title={t("Zoom in")}
          aria-label={t("Zoom in")}
          className="flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground disabled:pointer-events-none disabled:opacity-35"
        >
          <Plus className="size-4" />
        </button>
      </footer>
    </aside>
  );
}
