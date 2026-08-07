/**
 * Turning a stored bounding box into pixels on a rendered page.
 *
 * The boxes have been carried since F5 — through parsing, chunking, retrieval and
 * generation — for this one calculation, and they were stored **normalised** (0..1 relative
 * to page size) precisely so it stays this small. Absolute PDF points would have to be
 * rescaled by the page's own dimensions at every zoom level, and would break the day a
 * viewer decided to render at a different scale.
 *
 * Kept separate from the React component because it is arithmetic, and arithmetic that
 * silently produces a rectangle in the wrong place is the kind of bug a rendering test
 * cannot see but a unit test can.
 */

export interface Box {
  /** Which page this box is on. Present because a chunk may span a page boundary. */
  page?: number;
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface Rect {
  left: number;
  top: number;
  width: number;
  height: number;
}

/**
 * One normalised box against a rendered page of the given pixel size.
 *
 * Coordinates are clamped to the page. A box slightly outside it is not worth refusing —
 * extraction rounds, and a highlight one pixel over the edge is better than no highlight —
 * but one that escapes entirely would draw over the surrounding UI.
 */
export function toRect(box: Box, pageWidth: number, pageHeight: number): Rect {
  const left = clamp(Math.min(box.x0, box.x1));
  const right = clamp(Math.max(box.x0, box.x1));
  const top = clamp(Math.min(box.y0, box.y1));
  const bottom = clamp(Math.max(box.y0, box.y1));

  return {
    left: left * pageWidth,
    top: top * pageHeight,
    width: (right - left) * pageWidth,
    height: (bottom - top) * pageHeight,
  };
}

function clamp(value: number): number {
  return Math.min(1, Math.max(0, value));
}

/**
 * The boxes belonging to one page of a citation.
 *
 * A chunk can straddle a page break, so its boxes are not all on the page the citation
 * names. Boxes with no `page` are assumed to be on the citation's own page — that is what
 * the older ingestion runs produced, and dropping them would silently stop highlighting
 * documents ingested before the field existed.
 */
export function boxesOnPage(boxes: Box[], page: number): Box[] {
  return boxes.filter((box) => box.page === undefined || box.page === page);
}

/**
 * Where to scroll so the highlight is visible, given the viewport height.
 *
 * Positions the first box roughly a third of the way down rather than at the very top: a
 * highlight flush against the edge of the viewport reads as cut off, and the surrounding
 * lines are what let a reader confirm the citation actually says what the answer claimed.
 */
export function scrollTargetFor(rect: Rect, pageTop: number, viewportHeight: number): number {
  return Math.max(0, pageTop + rect.top - viewportHeight / 3);
}
