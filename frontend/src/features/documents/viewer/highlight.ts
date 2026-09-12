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

/**
 * The whole passage on one page, as one rectangle.
 *
 * A chunk is highlighted line by line — a paragraph is fourteen boxes, not one — so every
 * question about "where the passage is" has to be asked of their union. Framing on the first
 * box alone puts a twenty-line passage's opening words in the middle of the panel with the
 * rest of it below the fold, which is the same hunting problem `scrollTargetFor` was written
 * to remove, one level up.
 */
export function unionRect(rects: Rect[]): Rect | null {
  if (!rects.length) return null;

  const left = Math.min(...rects.map((rect) => rect.left));
  const top = Math.min(...rects.map((rect) => rect.top));
  const right = Math.max(...rects.map((rect) => rect.left + rect.width));
  const bottom = Math.max(...rects.map((rect) => rect.top + rect.height));

  return { left, top, width: right - left, height: bottom - top };
}

/**
 * Where to scroll so the *whole* passage is framed, rather than merely visible.
 *
 * Two cases, and they are different questions. A passage that fits in the panel should be
 * centred: there is room to show all of it, and centring is what makes a reader see it as one
 * block rather than as text that happens to start here. A passage taller than the panel cannot
 * be centred — centring it would put its first line above the top edge — so it falls back to
 * the same third-down rule `scrollTargetFor` uses, which keeps the opening line where reading
 * starts.
 *
 * Separate from `scrollTargetFor` rather than replacing it. That one is what an ordinary
 * citation click does, and it is deliberately gentler: it moves the page as little as it can.
 * This one is for the framed open, where the point is composition.
 */
export function frameTargetFor(rect: Rect, pageTop: number, viewportHeight: number): number {
  const centred = rect.height <= viewportHeight;
  const offset = centred ? (viewportHeight - rect.height) / 2 : viewportHeight / 3;

  return Math.max(0, pageTop + rect.top - offset);
}

/**
 * Horizontally, the page is centred and that is the whole rule.
 *
 * A column of body text sits in the middle of its page in every document this product has
 * been pointed at, so centring the page centres the text — and unlike a column-derived
 * offset, it cannot be thrown off by a marginal note, a page number or a stamp that happens
 * to be the widest box on the page.
 *
 * Clamped at zero because a page narrower than the panel has nothing to scroll: the negative
 * half would be silently discarded by the browser, which is the right result reached by
 * accident rather than on purpose.
 */
export function centredScrollLeft(contentWidth: number, viewportWidth: number): number {
  return Math.max(0, (contentWidth - viewportWidth) / 2);
}

/**
 * The zoom that makes the passage's own column fill the panel.
 *
 * Fitting the *page* to the panel is the obvious rule and it is the wrong one: it shows the
 * margins, which nobody is reading, and leaves the text smaller than it was. What a reader
 * is checking is the column the highlighted lines sit in, so that is what is fitted — measured
 * from the widest highlighted line, which is the one piece of the page whose width is known
 * without parsing the document's layout.
 *
 * `fill` leaves a hair of margin either side. At exactly 1 the column touches both edges,
 * which reads as text that has been cropped rather than framed.
 *
 * Returns the caller's current zoom unchanged when there is nothing to measure — a citation
 * with no boxes, or a page still being laid out at zero width.
 */
export function zoomForColumn(
  columnWidth: number,
  availableWidth: number,
  currentZoom: number,
  fill = 0.97,
): number {
  if (columnWidth <= 0 || availableWidth <= 0) return currentZoom;

  return (currentZoom * (availableWidth * fill)) / columnWidth;
}
