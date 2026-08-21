/**
 * Knowing which result you are looking at.
 *
 * Clicking a passage opened the PDF panel and left the list exactly as it was, so after one
 * scroll there was nothing on screen connecting the page in the viewer to the row that
 * asked for it — on a list of near-identical cards, all filename, page number and two lines
 * of grey text.
 *
 * The mark is driven by the parent's citation rather than by a click handler here, and that
 * is the part worth protecting: closing the viewer has to unmark the row, and only a single
 * source of truth gets that right for free.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Search } from "./Search";
import type { SearchHit } from "./api";

const hit = (id: string, filename: string): SearchHit => ({
  chunk_id: id,
  document_id: `doc-${id}`,
  filename,
  page_num: 3,
  text: "a passage about severance pay",
  bboxes: [],
  label_ids: [],
  lexical_rank: 1,
  dense_rank: 1,
  score: 0.5,
  lexical_score: 0.5,
  dense_score: 0.5,
  rerank_score: null,
});

const results = vi.fn();
vi.mock("./api", () => ({ search: (...args: unknown[]) => results(...args) }));

const run = async (openChunkId: string | null = null) => {
  const onCitation = vi.fn();
  results.mockResolvedValue({
    hits: [hit("one", "handbook.pdf"), hit("two", "contract.pdf")],
    degraded: false,
    reason: null,
    took_ms: 12,
  });

  const view = render(
    <Search token="t" onCitation={onCitation} openChunkId={openChunkId} searchable />,
  );
  const box = screen.getByLabelText("Search");
  fireEvent.change(box, { target: { value: "severance" } });
  fireEvent.submit(box.closest("form")!);
  // The filename sits beside a nested span, so the row is found by its card rather than by
  // a text match that the element boundary would break.
  await screen.findByText(/2 passages/);
  return { onCitation, view };
};

/** The result cards, in order. */
const cards = () =>
  screen
    .getAllByRole("button")
    .filter((element) => element.textContent?.includes(".pdf"));

describe("marking the open result", () => {
  it("marks nothing while the viewer is closed", async () => {
    await run(null);

    expect(cards().some((card) => card.getAttribute("aria-current") === "true")).toBe(false);
  });

  it("marks the result the viewer is showing", async () => {
    await run("two");

    const marked = cards().filter((card) => card.getAttribute("aria-current") === "true");
    expect(marked).toHaveLength(1);
    expect(marked[0]?.textContent).toContain("contract.pdf");
  });

  it("marks exactly one, so the list never claims two are open", async () => {
    await run("one");

    expect(cards().filter((card) => card.getAttribute("aria-current") === "true")).toHaveLength(1);
  });

  it("announces the selection rather than only colouring it", async () => {
    // Two greys apart is not a distinction everybody can make, and a screen reader is
    // given nothing at all by a background colour.
    await run("one");

    expect(cards()[0]?.getAttribute("aria-current")).toBe("true");
  });

  it("clears the mark when the viewer closes", async () => {
    // The reason the open chunk is a prop. A selection remembered inside this list would
    // stay lit over a panel that is no longer on screen.
    const { view } = await run("one");
    expect(cards()[0]?.getAttribute("aria-current")).toBe("true");

    view.rerender(<Search token="t" onCitation={vi.fn()} openChunkId={null} searchable />);

    expect(cards().some((card) => card.getAttribute("aria-current") === "true")).toBe(false);
  });

  it("still reports the click to the parent", async () => {
    // The mark is a consequence of the citation, not a replacement for raising it.
    const { onCitation } = await run(null);

    fireEvent.click(cards()[1]!);

    expect(onCitation).toHaveBeenCalledWith(expect.objectContaining({ chunk_id: "two" }));
  });
});
