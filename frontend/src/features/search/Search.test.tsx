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

// The label catalogue is fetched over the network by the real module. Stubbed so a hit can
// carry a name and its chip renders as the *button* it becomes when a filter is offered —
// which is the whole point of the nesting guard at the bottom of this file.
vi.mock("@/features/labels", async () => {
  const actual = await vi.importActual<typeof import("@/features/labels")>("@/features/labels");
  return {
    ...actual,
    labels: vi.fn().mockResolvedValue([{ id: "l1", name: "legal/contracts" }]),
  };
});

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

    // The question travels with the citation, so the panel it opens can start a
    // conversation without asking the reader to type what they just searched for.
    expect(onCitation).toHaveBeenCalledWith(
      expect.objectContaining({ chunk_id: "two" }),
      "severance",
    );
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

    // The third argument is not incidental: it is what separates this open from a citation
    // the reader clicked. The shell reads it to collapse the sidebar, widen the panel and
    // compose the passage in it — and doing any of that to a reader's own click would be the
    // interface rearranging a screen they are already reading.
    expect(onCitation).toHaveBeenCalledWith(
      expect.objectContaining({ chunk_id: "one" }),
      "severance",
      true,
    );
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

  it("puts no control inside another control", async () => {
    // The guard, not the symptom. A result card used to be one button wrapping the whole
    // row, and a `TagChip` carrying a filter renders a button of its own — so the tree had
    // a control nested in a control. React warns about it, but only in development, and a
    // warning in a console nobody has open is not a guard: this is the one that fails in
    // CI if the card is ever wrapped in a button again.
    //
    // Asserted on the rendered DOM rather than on the markup, because the nesting is what
    // a screen reader chokes on and only the output shows it. `button button` matches at
    // any depth, which is the point — a wrapper two levels up breaks it just the same.
    results.mockResolvedValue({
      hits: [{ ...hit("one", "handbook.pdf"), label_ids: ["l1"] }],
      degraded: false,
      reason: null,
      took_ms: 12,
    });

    // `onSelectTag` is what turns a chip from a span into a button, so it has to be here or
    // the assertion passes without ever rendering the thing it guards against.
    const { container } = render(
      <Search token="t" onCitation={vi.fn()} openChunkId={null} searchable onSelectTag={vi.fn()} />,
    );
    const box = screen.getByLabelText("Search");
    fireEvent.change(box, { target: { value: "severance" } });
    fireEvent.submit(box.closest("form")!);
    await screen.findByText(/1 passage/);

    // The chip really is a button — proving the guard below has something to catch.
    expect(screen.getByRole("button", { name: /contracts/i })).toBeTruthy();
    expect(container.querySelectorAll("button button")).toHaveLength(0);
  });
});

/**
 * The feature the whole product turns on: search that can say the corpus has nothing.
 *
 * The dense half returns the k nearest neighbours however far away they are, so before this
 * a question about Messi's goals over a corpus of employment law came back with eight ranked
 * passages and every appearance of having found something.
 *
 * Both states are asserted, and the pair is the point. `weak` must **show** its results —
 * hiding a passage that was really there leaves the reader concluding the corpus does not
 * contain it, with no way to find out otherwise, and that is the expensive failure. `none`
 * must not offer the rewording advice, which is good counsel when the query missed and
 * useless when the subject is absent.
 */
describe("when the corpus has little or nothing to say", () => {
  const ask = async (result: Partial<Parameters<typeof results.mockResolvedValue>[0]>) => {
    results.mockResolvedValue({
      hits: [],
      degraded: false,
      reason: null,
      took_ms: 9,
      ...result,
    } as never);
    render(<Search token="t" onCitation={vi.fn()} searchable />);
    const box = screen.getByLabelText("Search");
    fireEvent.change(box, { target: { value: "cuantos goles marco Messi" } });
    fireEvent.submit(box.closest("form")!);
  };

  it("says the match is poor and still shows the passages", async () => {
    await ask({ hits: [hit("one", "handbook.pdf")], relevance: "weak" });

    expect(await screen.findByText(/Nothing matches this closely/)).toBeTruthy();
    // Still on screen. The notice qualifies them; it does not replace them.
    expect(screen.getByText(/handbook\.pdf/)).toBeTruthy();
  });

  it("says the corpus does not cover it, and does not suggest rewording", async () => {
    await ask({ hits: [], relevance: "none" });

    expect(await screen.findByText(/Nothing in your documents is about/)).toBeTruthy();
    // The generic empty state's advice is good when the wording missed and useless here:
    // no phrasing makes an absent subject appear.
    expect(screen.queryByText(/Try fewer words/)).toBeNull();
  });

  it("falls back to the ordinary empty state when the server says nothing", async () => {
    // An older server sends no `relevance`. Silence is read as `confident`, so the screen
    // behaves exactly as it did before this feature existed.
    await ask({ hits: [] });

    expect(await screen.findByText(/Nothing matched that query/)).toBeTruthy();
  });
});

/**
 * The corpus emptying out under a mounted screen.
 *
 * `searchable` is `status?.searchable ?? true` in the shell: true while `/tenant/status` is
 * still in flight, and whatever the server says once it answers. So every session that
 * reaches a corpus with nothing in it — a fresh installation, and a token left in a browser
 * for a tenant that no longer exists — renders this screen once as searchable and again as
 * not. That is a rerender, not a remount, and this screen used to run one fewer hook on the
 * second pass: the prefill effect sat *below* the `if (!searchable)` return. React counts
 * hooks, so it took the whole application down with "Rendered fewer hooks than expected"
 * (#300 in a production build) rather than showing the empty-corpus notice.
 */
describe("when the corpus turns out to be empty after it has rendered", () => {
  it("swaps to the notice instead of crashing", async () => {
    const view = render(<Search token="t" onCitation={vi.fn()} searchable />);
    await screen.findByLabelText("Search");

    // The same component, told the corpus is empty. Throwing here is the bug.
    view.rerender(<Search token="t" onCitation={vi.fn()} searchable={false} />);

    expect(screen.getByText(/no documents to search yet/i)).toBeTruthy();
    // And back again, because a document finishing ingestion is the same flip in reverse.
    view.rerender(<Search token="t" onCitation={vi.fn()} searchable />);
    expect(screen.getByLabelText("Search")).toBeTruthy();
  });
});
