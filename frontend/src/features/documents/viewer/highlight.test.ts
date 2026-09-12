import { describe, expect, it } from "vitest";

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

const box = (over: Partial<Box> = {}): Box => ({ x0: 0.1, y0: 0.2, x1: 0.9, y1: 0.25, ...over });

describe("bounding boxes on a rendered page", () => {
  it("scales a normalised box to the rendered size", () => {
    const rect = toRect(box(), 1000, 2000);

    // `closeTo` rather than exact equality: 0.25 - 0.2 is not exactly 0.05 in binary
    // floating point, and rounding inside `toRect` to make a test pretty would throw away
    // sub-pixel precision the browser is perfectly happy to use.
    expect(rect.left).toBeCloseTo(100);
    expect(rect.top).toBeCloseTo(400);
    expect(rect.width).toBeCloseTo(800);
    expect(rect.height).toBeCloseTo(100);
  });

  it("scales with the page, which is why the boxes are stored normalised", () => {
    // The same box at two zoom levels. Absolute PDF points would need the page's own
    // dimensions to rescale, and would land in the wrong place the moment a viewer chose a
    // different scale.
    const small = toRect(box(), 500, 1000);
    const large = toRect(box(), 1000, 2000);

    expect(large.left).toBe(small.left * 2);
    expect(large.width).toBe(small.width * 2);
  });

  it("normalises a box whose corners arrive reversed", () => {
    // Extractors disagree about which corner comes first, and a negative width draws
    // nothing at all — a silent failure that looks like a missing highlight.
    const rect = toRect({ x0: 0.9, y0: 0.25, x1: 0.1, y1: 0.2 }, 1000, 2000);

    expect(rect.width).toBeCloseTo(800);
    expect(rect.height).toBeCloseTo(100);
    expect(rect.left).toBeCloseTo(100);
  });

  it("clamps a box that escapes the page", () => {
    // Extraction rounds, so a box slightly over the edge is normal and worth drawing. One
    // that escapes entirely would paint over the surrounding interface.
    const rect = toRect({ x0: -0.5, y0: 0.9, x1: 1.4, y1: 1.6 }, 1000, 2000);

    expect(rect.left).toBe(0);
    expect(rect.width).toBe(1000);
    expect(rect.top + rect.height).toBeLessThanOrEqual(2000);
  });

  it("keeps only the boxes on the page being rendered", () => {
    // A chunk can straddle a page break, so not every box belongs to the page the citation
    // names.
    const boxes = [box({ page: 1 }), box({ page: 2 }), box({ page: 2, x0: 0.2 })];

    expect(boxesOnPage(boxes, 2)).toHaveLength(2);
  });

  it("keeps boxes with no page, for documents ingested before the field existed", () => {
    // Dropping them would silently stop highlighting older documents, and the symptom
    // would be "highlighting is broken" rather than "that document predates the field".
    const boxes = [box(), box({ page: 3 })];

    expect(boxesOnPage(boxes, 1)).toHaveLength(1);
  });

  it("scrolls so the highlight sits below the top edge", () => {
    // Flush against the top reads as cut off, and the surrounding lines are what let a
    // reader confirm the citation says what the answer claimed.
    const rect = toRect(box(), 1000, 2000);

    const target = scrollTargetFor(rect, 5000, 900);

    expect(target).toBeCloseTo(5000 + 400 - 300);
  });

  it("never scrolls above the top of the document", () => {
    const rect = toRect({ x0: 0, y0: 0, x1: 1, y1: 0.05 }, 1000, 2000);

    expect(scrollTargetFor(rect, 0, 900)).toBe(0);
  });
});

describe("framing a passage rather than merely reaching it", () => {
  it("takes the whole passage, not its first line", () => {
    // A paragraph arrives as one box per line. Measured on a real citation: fourteen of them.
    const rects = [
      { left: 100, top: 800, width: 400, height: 20 },
      { left: 50, top: 830, width: 700, height: 20 },
      { left: 50, top: 860, width: 600, height: 20 },
    ];

    const union = unionRect(rects)!;

    expect(union.left).toBe(50);
    expect(union.top).toBe(800);
    expect(union.width).toBe(700);
    expect(union.height).toBe(80);
  });

  it("has nothing to frame when there are no boxes", () => {
    expect(unionRect([])).toBeNull();
  });

  it("centres a passage that fits in the panel", () => {
    // 900 tall panel, 80 tall passage: there is room for all of it, so it goes in the middle
    // rather than a third down. (900 - 80) / 2 = 410.
    const rect = { left: 0, top: 1000, width: 500, height: 80 };

    expect(frameTargetFor(rect, 0, 900)).toBeCloseTo(1000 - 410);
  });

  it("falls back to a third down when the passage is taller than the panel", () => {
    // Centring here would put the passage's first line above the top edge — the reader would
    // arrive in the middle of the thing they clicked to read.
    const rect = { left: 0, top: 1000, width: 500, height: 1200 };

    expect(frameTargetFor(rect, 0, 900)).toBeCloseTo(1000 - 300);
  });

  it("never frames above the top of the document", () => {
    const rect = { left: 0, top: 10, width: 500, height: 20 };

    expect(frameTargetFor(rect, 0, 900)).toBe(0);
  });
});

describe("centring the page horizontally", () => {
  it("splits the overflow evenly", () => {
    // The measured case: a 1169px content box in an 843px panel centres at 163.
    expect(centredScrollLeft(1169, 843)).toBeCloseTo(163);
  });

  it("does not scroll a page that already fits", () => {
    expect(centredScrollLeft(600, 843)).toBe(0);
  });
});

describe("the zoom that fills the panel with the passage's column", () => {
  it("grows the page until the column spans the panel", () => {
    // A column drawn 400px wide at zoom 1, in 800px of panel, has to roughly double.
    expect(zoomForColumn(400, 800, 1)).toBeCloseTo(1.94);
  });

  it("shrinks a column that overflows the panel", () => {
    expect(zoomForColumn(1000, 500, 1)).toBeLessThan(1);
  });

  it("is relative to the zoom the column was measured at", () => {
    // The column width comes off the rendered page, so it already carries whatever zoom that
    // page was drawn at. Ignoring that would square the factor on the second call.
    expect(zoomForColumn(400, 800, 2)).toBeCloseTo(zoomForColumn(400, 800, 1) * 2);
  });

  it("leaves the zoom alone when there is nothing to measure", () => {
    // A citation with no bounding boxes, or a panel that has not been laid out yet.
    expect(zoomForColumn(0, 800, 1.25)).toBe(1.25);
    expect(zoomForColumn(400, 0, 1.25)).toBe(1.25);
  });
});
