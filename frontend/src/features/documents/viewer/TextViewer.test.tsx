/**
 * The citation viewer for documents with no pages.
 *
 * The assertion that matters is not "it renders the file". It is that the underline lands
 * on the passage the answer cited — a highlight in the wrong place is worse than none,
 * because it looks like the product read something it did not.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { TextViewer } from "./TextViewer";
import type { Citation } from "@/features/chat";

const BODY = [
  "# Collector runbook",
  "",
  "Restart the collector before the reconciler, never the other way round.",
  "",
  "Page the on-call engineer if the backlog exceeds four hours.",
].join("\n");

const PASSAGE = "Restart the collector before the reconciler, never the other way round.";

function citation(overrides: Partial<Citation> = {}): Citation {
  return {
    marker: 1,
    chunk_id: "c1",
    document_id: "d1",
    filename: "runbook.md",
    media_type: "text/markdown",
    page_num: null,
    char_start: BODY.indexOf(PASSAGE),
    char_end: BODY.indexOf(PASSAGE) + PASSAGE.length,
    text: PASSAGE,
    bboxes: [],
    ...overrides,
  };
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(BODY, { status: 200 })),
  );
});

describe("opening a text citation", () => {
  it("underlines the passage the answer cited, and only that", async () => {
    render(<TextViewer citation={citation()} token="t" />);

    const mark = await screen.findByTestId("citation-highlight");
    expect(mark.textContent).toBe(PASSAGE);
  });

  it("shows the whole document around it", async () => {
    render(<TextViewer citation={citation()} token="t" />);

    const body = await screen.findByTestId("text-body");
    expect(body.textContent).toBe(BODY);
  });

  it("never claims a page number", async () => {
    // The reason `chunks.page_num` was made nullable rather than defaulted to 1. A viewer
    // that prints "Page 1" over a Markdown file is the lie the null exists to prevent.
    //
    // Scoped to the chrome, not the render: this runbook contains the sentence "Page the
    // on-call engineer", and a match anywhere on screen would be satisfied by the
    // document's own words. The first version of this test was, which is the failure mode
    // where a passing assertion says nothing.
    const { container } = render(<TextViewer citation={citation()} token="t" />);

    await screen.findByTestId("text-body");
    const header = container.querySelector("header");
    expect(header?.textContent).toMatch(/Characters/);
    expect(header?.textContent).not.toMatch(/page/i);
  });

  it("asks for the file with the caller's token", async () => {
    render(<TextViewer citation={citation()} token="secret-token" />);

    await screen.findByTestId("text-body");
    expect(fetch).toHaveBeenCalledWith(
      "/documents/d1/file",
      expect.objectContaining({ headers: { Authorization: "Bearer secret-token" } }),
    );
  });

  it("says the document could not be opened rather than showing an empty pane", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("nope", { status: 404 })),
    );

    render(<TextViewer citation={citation()} token="t" />);

    expect((await screen.findByRole("alert")).textContent).toMatch(/could not be opened/i);
  });
});

describe("offsets that do not fit the file", () => {
  /**
   * The document behind a citation can change — re-uploaded under the same id, or a stale
   * tab left open across an ingestion. `String.slice` answers an out-of-range request with
   * an empty string and no error, which would render as a citation highlighting nothing
   * while the header still claimed a range. Clamping makes the failure visible as a short
   * highlight rather than an invisible one.
   */
  it("clamps a range that runs past the end instead of highlighting nothing", async () => {
    render(
      <TextViewer citation={citation({ char_start: 10, char_end: 99_999 })} token="t" />,
    );

    const mark = await screen.findByTestId("citation-highlight");
    expect(mark.textContent).toBe(BODY.slice(10));
  });

  it("survives a reversed range without throwing", async () => {
    render(<TextViewer citation={citation({ char_start: 40, char_end: 10 })} token="t" />);

    const mark = await screen.findByTestId("citation-highlight");
    expect(mark.textContent).toBe("");
    // The document is still readable, which is the point: a bad range must not cost the
    // reader the source.
    expect((await screen.findByTestId("text-body")).textContent).toBe(BODY);
  });
});

describe("switching documents", () => {
  it("does not let a slower fetch paint over a newer one", async () => {
    // Two citations clicked quickly. Without the cancellation guard the first response
    // arrives last and the reader is looking at a document they did not click.
    const slow = new Response("the older document, arriving late", { status: 200 });
    const quick = new Response(BODY, { status: 200 });
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        call += 1;
        return call === 1 ? new Promise<Response>((r) => setTimeout(() => r(slow), 50)) : quick;
      }),
    );

    const view = render(<TextViewer citation={citation({ document_id: "old" })} token="t" />);
    view.rerender(<TextViewer citation={citation({ document_id: "new" })} token="t" />);

    await waitFor(async () =>
      expect((await screen.findByTestId("text-body")).textContent).toBe(BODY),
    );
    await new Promise((r) => setTimeout(r, 80));
    expect((await screen.findByTestId("text-body")).textContent).toBe(BODY);
  });
});
