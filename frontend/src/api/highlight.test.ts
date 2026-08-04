import { describe, expect, it } from "vitest";

import { boxesOnPage, scrollTargetFor, toRect, type Box } from "./highlight";

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
