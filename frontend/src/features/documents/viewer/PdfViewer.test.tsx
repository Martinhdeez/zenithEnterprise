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
 *
 * Zoom makes the second half of this file the more important one. The canvas can now be any
 * size, and the highlight is drawn *on top of* it rather than in it — so the two are held in
 * step by arithmetic, and arithmetic that quietly disagrees with itself would put an amber
 * box over the wrong sentence and tell nobody. The alignment assertions below are the guard
 * on that, and they are deliberately taken at scales that are not round multiples of
 * anything: a factor applied upside down still lines up perfectly at 1.0.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";

const getDocument = vi.fn();

vi.mock("pdfjs-dist", () => ({
  GlobalWorkerOptions: {},
  getDocument: (...args: unknown[]) => getDocument(...args),
}));
vi.mock("pdfjs-dist/build/pdf.worker.min.mjs?url", () => ({ default: "worker" }));

const { PdfViewer } = await import("./PdfViewer");

/** The component's own hardcoded scale. Zoom 1 means exactly this. */
const BASE_SCALE = 1.4;
/** The page as it comes out at zoom 1, which is what the pre-zoom assertions were written to. */
const PAGE = { width: 600, height: 800 };

/** Every scale pdf.js was asked for, in order. */
let asked: number[] = [];

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

/** The normalised box above, as the four fractions the highlight has to keep at every zoom. */
const BOX = { left: 0.1, top: 0.8, width: 0.8, height: 0.86 - 0.8 };

beforeEach(() => {
  asked = [];
  getDocument.mockReset();
  getDocument.mockReturnValue({
    promise: Promise.resolve({
      numPages: 10,
      getPage: async () => ({
        // The double honours the scale it is handed, because that is the thing under test.
        // A viewport that ignored it would let every assertion below pass against a viewer
        // that never zoomed.
        getViewport: ({ scale }: { scale: number }) => {
          asked.push(scale);
          return {
            width: (PAGE.width * scale) / BASE_SCALE,
            height: (PAGE.height * scale) / BASE_SCALE,
          };
        },
        // `cancel` because the viewer aborts a render in flight when the zoom settles again
        // before the previous one finished painting.
        render: () => ({ promise: Promise.resolve(), cancel: () => {} }),
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
 * Three things jsdom does not do, each of which would make these tests pass for the wrong
 * reason: every element has a zero height, so the scroll arithmetic would be `0 - 0/3`; every
 * element has a zero offset; and **scrolling is not implemented at all** — assigning
 * `scrollTop` on an element jsdom considers unscrollable is silently discarded, so reading it
 * back always gives zero however correct the component is.
 *
 * So the sizes are stubbed on the prototype before the render, where the effects will see
 * them, and `scrollTop` is replaced with an accessor that records what was written to it.
 */
const CONTENT_WIDTH = 1000;

function stubGeometry(): { scrolledTo: () => number; scrolledAcross: () => number } {
  const at = { top: 0, left: 0 };
  for (const [name, value] of [
    ["clientHeight", PANEL_HEIGHT],
    ["clientWidth", PANEL_HEIGHT],
    ["offsetTop", CANVAS_TOP],
    // The page is wider than the panel, which is the only case horizontal centring has an
    // answer for. jsdom lays nothing out, so `scrollWidth` would otherwise be zero and the
    // centring would be asserted against a page that fits.
    ["scrollWidth", CONTENT_WIDTH],
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
  for (const name of ["scrollTop", "scrollLeft"] as const) {
    const axis = name === "scrollTop" ? "top" : "left";
    Object.defineProperty(HTMLElement.prototype, name, {
      configurable: true,
      get: () => at[axis],
      set: (value: number) => {
        at[axis] = value;
      },
    });
  }
  return { scrolledTo: () => at.top, scrolledAcross: () => at.left };
}

/** Render with the geometry stubbed, and report where the viewer asked to scroll to. */
async function show(props: Record<string, unknown> = {}): Promise<number> {
  const geometry = stubGeometry();
  await act(async () => {
    render(<PdfViewer citation={citation(props) as never} token="t" />);
  });
  return geometry.scrolledTo();
}

/** Render and leave the tree in place for the zoom assertions. */
async function open(props: Record<string, unknown> = {}): Promise<void> {
  stubGeometry();
  await act(async () => {
    render(<PdfViewer citation={citation(props) as never} token="t" />);
  });
}

/**
 * Where the highlight sits, **as a fraction of the page it is drawn on**.
 *
 * This is the only form of the question worth asking. Absolute pixels change with every
 * zoom by design; the fraction is what must not move, because it is the normalised box the
 * ingestion pipeline stored and the reason the box was stored normalised at all.
 */
function alignment() {
  const sheet = screen.getByTestId("pdf-canvas") as HTMLCanvasElement;
  const mark = screen.getByTestId("citation-highlight");
  const px = (value: string) => Number.parseFloat(value);

  return {
    page: { width: sheet.width, height: sheet.height },
    left: px(mark.style.left) / sheet.width,
    top: px(mark.style.top) / sheet.height,
    width: px(mark.style.width) / sheet.width,
    height: px(mark.style.height) / sheet.height,
  };
}

function press(label: string): Promise<void> {
  return act(async () => {
    fireEvent.click(screen.getByLabelText(label));
  });
}

/** A wheel notch over the page, and the pause that lets the sharp redraw land. */
async function wheel(over: Partial<WheelEventInit> = {}): Promise<WheelEvent> {
  const event = new WheelEvent("wheel", { deltaY: -100, cancelable: true, bubbles: true, ...over });
  await act(async () => {
    screen.getByTestId("pdf-scroller").dispatchEvent(event);
  });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 250));
  });
  return event;
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

describe("the page is drawn at the zoom that was asked for", () => {
  it("starts at the scale the viewer has always used", async () => {
    await open();

    expect(asked.at(-1)!).toBeCloseTo(BASE_SCALE);
    expect(alignment().page).toEqual({ width: PAGE.width, height: PAGE.height });
  });

  it("redraws bigger, and smaller, and finds its way back to the default", async () => {
    await open();

    await press("Zoom in");
    expect(alignment().page.width).toBeGreaterThan(PAGE.width);

    // Back down past the default, then up to it again. The buttons step between fixed rungs
    // precisely so that 1 stays reachable from wherever a pinch happened to stop.
    await press("Zoom out");
    await press("Zoom out");
    expect(alignment().page.width).toBeLessThan(PAGE.width);

    await press("Zoom in");
    expect(asked.at(-1)!).toBeCloseTo(BASE_SCALE);
    expect(alignment().page).toEqual({ width: PAGE.width, height: PAGE.height });
  });

  it("stops at both ends", async () => {
    // `.disabled` rather than a `jest-dom` matcher: this project configures none.
    const button = (label: string) => screen.getByLabelText(label) as HTMLButtonElement;
    await open();

    for (let pressed = 0; pressed < 12; pressed += 1) await press("Zoom in");
    expect(asked.at(-1)!).toBeCloseTo(BASE_SCALE * 4);
    expect(button("Zoom in").disabled).toBe(true);

    for (let pressed = 0; pressed < 12; pressed += 1) await press("Zoom out");
    expect(asked.at(-1)!).toBeCloseTo(BASE_SCALE * 0.5);
    expect(button("Zoom out").disabled).toBe(true);
  });
});

/**
 * The gate on this feature.
 *
 * The highlight is positioned over the canvas, not inside it, so nothing about the DOM forces
 * the two to agree — only the arithmetic does. If the canvas scales and the overlay does not
 * scale with it by exactly the same factor, the viewer points a reader at a sentence that is
 * not the one the answer used, and there is no symptom: the box still looks like a highlight.
 */
describe("the highlight stays on the text it marks, at every zoom", () => {
  /** The four fractions the stored box turns into, whatever the page is drawn at. */
  function expectAligned() {
    const at = alignment();
    expect(at.left).toBeCloseTo(BOX.left, 3);
    expect(at.top).toBeCloseTo(BOX.top, 3);
    expect(at.width).toBeCloseTo(BOX.width, 3);
    expect(at.height).toBeCloseTo(BOX.height, 3);
    return at;
  }

  it("holds at the default", async () => {
    await open();

    expectAligned();
  });

  it("holds at a scale that is not a round multiple of the hardcoded 1.4", async () => {
    // 1.4 * 1.25 = 1.75. A factor applied upside down — divided where it should be
    // multiplied, or measured against the page's unzoomed size — still lands perfectly at
    // 1.0, so proving it there proves nothing. Here the two answers differ by 40% of the
    // page width.
    await open();
    await press("Zoom in");

    const at = expectAligned();
    expect(at.page.width).toBeCloseTo((PAGE.width * BASE_SCALE * 1.25) / BASE_SCALE);
  });

  it("holds at an awkward scale nobody chose, which is what the wheel produces", async () => {
    // The wheel is exponential and continuous: this lands on 1.4 * e^0.18 = 1.6759…, a
    // number with no relationship to 1.4, to 1, or to any rung of the button ladder.
    await open();
    await wheel({ deltaY: -100 });

    const scale = asked.at(-1)!;
    expect(scale).not.toBeCloseTo(BASE_SCALE);
    expect(scale / BASE_SCALE).not.toBeCloseTo(Math.round(scale / BASE_SCALE));
    expectAligned();
  });

  it("holds when zoomed out below the default", async () => {
    // 1.4 * 0.8 = 1.12. Below zoom 1 a wrong factor errs in the opposite direction, which is
    // the other half of the case the awkward number above is chosen for.
    await open();
    await press("Zoom out");

    expect(asked.at(-1)!).toBeCloseTo(BASE_SCALE * 0.8);
    expectAligned();
  });

  it("holds while a pinch is still moving, because both live in one scaled box", async () => {
    // Between the gesture and the sharp redraw the page is a CSS transform, and that is the
    // structural reason the overlay cannot drift: the canvas and every highlight are inside
    // the element being transformed, so there is one scale factor, not two. A change that
    // put the transform on the canvas alone would fail here.
    await open();

    await act(async () => {
      screen
        .getByTestId("pdf-scroller")
        .dispatchEvent(
          new WheelEvent("wheel", { deltaY: -100, ctrlKey: true, cancelable: true, bubbles: true }),
        );
    });

    const scaled = screen.getByTestId("pdf-sheet");
    expect(scaled.style.transform).toMatch(/^scale\(/);
    expect(scaled.contains(screen.getByTestId("pdf-canvas"))).toBe(true);
    expect(scaled.contains(screen.getByTestId("citation-highlight"))).toBe(true);
    expectAligned();
  });
});

describe("the ways to zoom", () => {
  it("takes a plain wheel", async () => {
    await open();

    const before = alignment().page.width;
    await wheel({ deltaY: -100 });

    expect(alignment().page.width).toBeGreaterThan(before);
  });

  it("takes a trackpad pinch, which arrives as a wheel with ctrl held", async () => {
    await open();

    const before = alignment().page.width;
    await wheel({ deltaY: -20, ctrlKey: true });

    expect(alignment().page.width).toBeGreaterThan(before);
  });

  it("keeps the pinch off the browser's own page zoom", async () => {
    // Unprevented, a ctrl-wheel scales the whole application underneath the document, which
    // is not zooming a document — it is zooming everything except the document's canvas.
    await open();

    const pinch = await wheel({ deltaY: -20, ctrlKey: true });

    expect(pinch.defaultPrevented).toBe(true);
  });

  it("redraws once when the gesture settles, not once per notch", async () => {
    // Twenty notches is half a second of trackpad. Each one is a full pdf.js render of a
    // canvas up to sixteen megapixels if it is not held back.
    await open();
    const drawn = asked.length;

    await act(async () => {
      for (let notch = 0; notch < 20; notch += 1) {
        screen
          .getByTestId("pdf-scroller")
          .dispatchEvent(new WheelEvent("wheel", { deltaY: -10, cancelable: true, bubbles: true }));
      }
    });
    expect(asked.length).toBe(drawn);

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 250));
    });
    expect(asked.length).toBe(drawn + 1);
  });
});

/**
 * The composed open, which is what a search performs on its own.
 *
 * Three things have to happen together and none of them may happen to a citation the reader
 * clicked: the page is zoomed until the highlighted column fills the panel, it is centred
 * horizontally, and the whole passage is framed rather than merely reached.
 */
describe("the open a search composes", () => {
  it("zooms so the highlighted column fills the panel", async () => {
    // The box spans 0.8 of a 600px page — 480px — against 400px of panel less its 32px of
    // padding. The column has to come *down* to fit, which is the direction a naive
    // "zoom in to frame it" would get backwards.
    const geometry = stubGeometry();
    await act(async () => {
      render(<PdfViewer citation={citation() as never} token="t" framed />);
    });

    expect(asked.at(-1)!).toBeCloseTo((BASE_SCALE * (368 * 0.97)) / 480, 2);
    expect(geometry.scrolledAcross()).toBeCloseTo((CONTENT_WIDTH - PANEL_HEIGHT) / 2);
  });

  it("leaves the page alone when the reader opened the citation themselves", async () => {
    // The same citation, unframed: the scale it has always drawn at, and no horizontal move.
    const geometry = stubGeometry();
    await act(async () => {
      render(<PdfViewer citation={citation() as never} token="t" />);
    });

    expect(asked.at(-1)!).toBeCloseTo(BASE_SCALE);
    expect(geometry.scrolledAcross()).toBe(0);
  });

  it("does not fit a citation that has no boxes to measure", async () => {
    // Documents ingested before bounding boxes existed. There is no column to fill, and
    // guessing one would zoom to an arbitrary number on a page nobody asked to be zoomed.
    await act(async () => {
      render(<PdfViewer citation={citation({ bboxes: [] }) as never} token="t" framed />);
    });

    expect(asked.at(-1)!).toBeCloseTo(BASE_SCALE);
  });
});

describe("a document that is gone", () => {
  it("says so, rather than showing an empty page", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 404, arrayBuffer: async () => new ArrayBuffer(0) })),
    );

    await open();

    expect(screen.getByRole("alert").textContent).toBe("That document is no longer available.");
  });
});
