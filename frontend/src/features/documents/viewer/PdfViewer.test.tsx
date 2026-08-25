/**
 * The last step of the product's central interaction.
 *
 * mvp.md 2.9 sets the bar: a citation is not the string "page 34" — clicking it opens the PDF
 * on that page with the passage highlighted. Every layer since F5 carries normalised bounding
 * boxes for it.
 *
 * The panel is a third of the screen and the page is drawn at its natural width, so a passage
 * in the lower half of a page is below the fold. `scrollTargetFor` was written for exactly
 * that, tested twice, and called by nothing — so the reader had to hunt for the very thing
 * they clicked to see.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render } from "@testing-library/react";

const getDocument = vi.fn();

vi.mock("pdfjs-dist", () => ({
  GlobalWorkerOptions: {},
  getDocument: (...args: unknown[]) => getDocument(...args),
}));
vi.mock("pdfjs-dist/build/pdf.worker.min.mjs?url", () => ({ default: "worker" }));

const { PdfViewer } = await import("./PdfViewer");

const PAGE = { width: 600, height: 800 };

const citation = (overrides: Record<string, unknown> = {}) => ({
  marker: 1,
  chunk_id: "c1",
  document_id: "d1",
  filename: "handbook.pdf",
  page_num: 3,
  text: "a passage",
  // Normalised, and deliberately low on the page: the case the scroll exists for.
  bboxes: [{ page: 3, x0: 0.1, y0: 0.8, x1: 0.9, y1: 0.86 }],
  ...overrides,
});

beforeEach(() => {
  getDocument.mockReset();
  getDocument.mockReturnValue({
    promise: Promise.resolve({
      numPages: 10,
      getPage: async () => ({
        getViewport: () => PAGE,
        render: () => ({ promise: Promise.resolve() }),
      }),
    }),
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(8) })),
  );
});

const PANEL_HEIGHT = 400;
const CANVAS_TOP = 16;

/**
 * Render with the geometry stubbed, and report where the viewer asked to scroll to.
 *
 * Three things jsdom does not do, each of which would make this pass for the wrong reason:
 * every element has a zero height, so the arithmetic would be `0 - 0/3`; every element has a
 * zero offset; and **scrolling is not implemented at all** — assigning `scrollTop` on an
 * element jsdom considers unscrollable is silently discarded, so reading it back always gives
 * zero however correct the component is.
 *
 * So the sizes are stubbed on the prototype before the render, where the effect will see
 * them, and `scrollTop` is replaced with an accessor that records what was written to it.
 */
async function show(props: Record<string, unknown> = {}): Promise<number> {
  let scrolledTo = 0;
  for (const [name, value] of [
    ["clientHeight", PANEL_HEIGHT],
    ["offsetTop", CANVAS_TOP],
  ] as const) {
    Object.defineProperty(HTMLElement.prototype, name, {
      configurable: true,
      get: () => value,
    });
  }
  // jsdom implements no canvas context, so the component's `if (!context) return` bails
  // before it ever measures the page — and every assertion below would be about a viewer
  // that never rendered.
  Object.defineProperty(HTMLCanvasElement.prototype, "getContext", {
    configurable: true,
    value: () => ({}),
  });
  Object.defineProperty(HTMLElement.prototype, "scrollTop", {
    configurable: true,
    get: () => scrolledTo,
    set: (value: number) => {
      scrolledTo = value;
    },
  });

  await act(async () => {
    render(<PdfViewer citation={citation(props) as never} token="t" />);
  });
  return scrolledTo;
}

describe("a citation low on the page", () => {
  it("brings the highlight into view", async () => {
    expect(await show()).toBeGreaterThan(0);
  });

  it("leaves room above it rather than putting it flush against the edge", async () => {
    // The reason `scrollTargetFor` subtracts a third of the viewport: a highlight against the
    // top edge reads as cut off, and the lines around it are what let a reader confirm the
    // passage says what the answer claimed.
    // 0.8 * 800 + 16 - 400/3 ≈ 523
    expect(await show()).toBeCloseTo(0.8 * PAGE.height + CANVAS_TOP - PANEL_HEIGHT / 3, 0);
  });
});

describe("a citation with no boxes", () => {
  it("does not move the page", async () => {
    // Documents ingested before bounding boxes existed, and palette-opened documents, which
    // carry no passage at all. Scrolling somewhere arbitrary would be worse than not moving.
    expect(await show({ bboxes: [] })).toBe(0);
  });
});
