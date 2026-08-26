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
  media_type: "application/pdf",
  char_start: 0,
  char_end: 0,
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

/**
 * The ranking evidence, and who it is for.
 *
 * This is the one search in the product that can answer "why is this first" — which is the
 * argument the product is sold on, and precisely why it used to sit under every result as
 * four rows of decimals. In front of an audience that is the first thing the eye lands on
 * and the last thing a business reader can use.
 */
describe("ranking detail", () => {
  it("is hidden until somebody asks for it", async () => {
    await run();

    expect(screen.queryByText(/combined/)).toBeNull();
    expect(screen.getByRole("button", { name: "Why these results?" })).toBeTruthy();
  });

  it("appears for every result at once", async () => {
    await run();

    fireEvent.click(screen.getByRole("button", { name: "Why these results?" }));

    // Both results, not just the one that was clicked: the question is about the ranking,
    // and a single result cannot answer it.
    expect(screen.getAllByText(/combined/)).toHaveLength(2);
  });

  it("can be put away again", async () => {
    await run();
    const toggle = screen.getByRole("button", { name: "Why these results?" });

    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("button", { name: "Hide ranking detail" }));

    expect(screen.queryByText(/combined/)).toBeNull();
  });

  it("says which half of the search missed the passage", async () => {
    // The most informative case in the whole panel: a passage only one half found is the
    // hybrid search earning its keep, and a blank column says that where a missing row
    // would just look like an absent number.
    results.mockResolvedValue({
      hits: [{ ...hit("one", "handbook.pdf"), dense_rank: null, dense_score: null }],
      degraded: false,
      reason: null,
      took_ms: 12,
    });
    render(<Search token="t" onCitation={vi.fn()} openChunkId={null} searchable />);
    const box = screen.getByLabelText("Search");
    fireEvent.change(box, { target: { value: "severance" } });
    fireEvent.submit(box.closest("form")!);
    await screen.findByText(/1 passage/);

    fireEvent.click(screen.getByRole("button", { name: "Why these results?" }));

    expect(screen.getByText("meaning —")).toBeTruthy();
  });

  it("offers nothing to expand when nothing matched", async () => {
    results.mockResolvedValue({ hits: [], degraded: false, reason: null, took_ms: 4 });
    render(<Search token="t" onCitation={vi.fn()} openChunkId={null} searchable />);
    const box = screen.getByLabelText("Search");
    fireEvent.change(box, { target: { value: "nothing" } });
    fireEvent.submit(box.closest("form")!);
    await screen.findByText(/0 passages/);

    expect(screen.queryByRole("button", { name: "Why these results?" })).toBeNull();
  });
});

/**
 * The two dead ends.
 *
 * A search that matched nothing and a search that failed were both a sentence and no way
 * forward. In a demonstration the first is the moment the product looks broken, and its
 * commonest cause is a folder filter picked up two screens ago that the reader cannot see
 * from a list with nothing in it.
 */
describe("when a search comes back with nothing", () => {
  const empty = async (props: Partial<Parameters<typeof Search>[0]> = {}) => {
    results.mockResolvedValue({ hits: [], degraded: false, reason: null, took_ms: 4 });
    render(
      <Search token="t" onCitation={vi.fn()} openChunkId={null} searchable {...props} />,
    );
    const box = screen.getByLabelText("Search");
    fireEvent.change(box, { target: { value: "severance" } });
    fireEvent.submit(box.closest("form")!);
    await screen.findByText(/Nothing matched/);
  };

  it("names the folder it was confined to", async () => {
    await empty({ filterName: "Finance", onClearFilter: vi.fn() });

    expect(screen.getByText("Finance")).toBeTruthy();
  });

  it("offers a way out of the filter", async () => {
    const onClearFilter = vi.fn();
    await empty({ filterName: "Finance", onClearFilter });

    fireEvent.click(screen.getByRole("button", { name: /Search everything/ }));

    expect(onClearFilter).toHaveBeenCalled();
  });

  it("suggests wording instead when no filter is to blame", async () => {
    // Advice about terms, not encouragement: this half of the search matches words, and
    // offering "try rephrasing" would be advice for the other half.
    await empty();

    expect(screen.queryByRole("button", { name: /Search everything/ })).toBeNull();
    expect(screen.getByText(/Try fewer words/)).toBeTruthy();
  });
});

describe("when a search fails", () => {
  it("can be tried again without retyping it", async () => {
    // Usually a moment of trouble rather than a permanent one — a restarted container, a
    // connection that dropped — and the query is still on screen.
    results.mockRejectedValueOnce(new Error("boom"));
    render(<Search token="t" onCitation={vi.fn()} openChunkId={null} searchable />);
    const box = screen.getByLabelText("Search");
    fireEvent.change(box, { target: { value: "severance" } });
    fireEvent.submit(box.closest("form")!);
    await screen.findByRole("alert");

    results.mockResolvedValue({
      hits: [hit("one", "handbook.pdf")],
      degraded: false,
      reason: null,
      took_ms: 9,
    });
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByText(/1 passage/)).toBeTruthy();
    // The same query, not a blank one.
    expect(results).toHaveBeenLastCalledWith("t", "severance", undefined, expect.anything());
  });
});

describe("the source opens on its own", () => {
  /**
   * The preview panel is mounted only while a document is open, and deliberately so: an
   * empty panel holding "click a citation" spends a third of the viewport on an
   * instruction. The answer to that is not to leave the space empty — it is to put a source
   * in it the moment there is one, which is the product's own argument making itself.
   */
  it("opens the best passage without waiting to be clicked", async () => {
    const { onCitation } = await run(null);

    expect(onCitation).toHaveBeenCalledWith(expect.objectContaining({ chunk_id: "one" }));
  });

  it("opens the top result and nothing else", async () => {
    // Opening any other passage would be choosing for the reader, and opening several would
    // be a panel that flickers through them.
    const { onCitation } = await run(null);

    expect(onCitation).toHaveBeenCalledTimes(1);
  });

  it("opens nothing when the search found nothing", async () => {
    // A panel is worse than no panel when it has nothing to show. This is the case that
    // would leave the previous document on screen beside "no passages matched".
    const onCitation = vi.fn();
    results.mockResolvedValue({ hits: [], degraded: false, reason: null, took_ms: 4 });

    render(<Search token="t" onCitation={onCitation} openChunkId={null} searchable />);
    const box = screen.getByLabelText("Search");
    fireEvent.change(box, { target: { value: "nothing at all" } });
    fireEvent.submit(box.closest("form")!);
    await screen.findByText(/nothing matched/i);

    expect(onCitation).not.toHaveBeenCalled();
  });
});
