/**
 * Finding one question among hundreds.
 *
 * The two behaviours worth holding are about *not* asking the server too much and about not
 * lying with a cursor: typing sends one request rather than one per character, and changing
 * a filter starts from the top, because a cursor names a position in the result set that
 * produced it and means nothing in a narrower one.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { History } from "./History";

const entry = (id: string, question: string, mine = true) => ({
  query_id: id,
  question,
  answer: "an answer",
  model_used: "stub",
  citations: 1,
  latency_retrieval_ms: 100,
  latency_generation_ms: 900,
  created_at: "2026-08-08T10:00:00Z",
  mine,
});

const history = vi.fn();

vi.mock("./api", () => ({ history: (...a: unknown[]) => history(...a) }));

beforeEach(() => {
  vi.clearAllMocks();
  history.mockResolvedValue({ entries: [entry("q1", "what is my severance?")], next_cursor: null });
});

/** Past the 250 ms debounce, with real timers so React flushes the state it sets. */
async function settle() {
  await new Promise((resolve) => setTimeout(resolve, 320));
}

describe("searching", () => {
  it("does not send a request per keystroke", async () => {
    // A query per character, on a table that grows with every question anybody asks.
    render(<History token="t" onAsk={vi.fn()} />);
    await settle();
    history.mockClear();

    const box = screen.getByLabelText("Search questions");
    for (const chunk of ["s", "se", "sev", "seve"]) {
      fireEvent.change(box, { target: { value: chunk } });
    }

    // One request for four keystrokes, carrying the last of them.
    await waitFor(() =>
      expect(history).toHaveBeenLastCalledWith("t", null, {
        search: "seve",
        mine: false,
        unanswered: false,
      }),
    );
    expect(history).toHaveBeenCalledTimes(1);
  });

  it("sends what was typed once it settles", async () => {
    render(<History token="t" onAsk={vi.fn()} />);
    await settle();

    fireEvent.change(screen.getByLabelText("Search questions"), { target: { value: "sever" } });
    await settle();

    await waitFor(() =>
      expect(history).toHaveBeenLastCalledWith("t", null, {
        search: "sever",
        mine: false,
        unanswered: false,
      }),
    );
  });

  it("starts from the top rather than resuming a cursor", async () => {
    // A cursor names a position in the result set that produced it. Resuming a narrowed
    // list from it would start partway down a list nobody has seen the beginning of.
    render(<History token="t" onAsk={vi.fn()} />);
    await settle();

    fireEvent.change(screen.getByLabelText("Search questions"), { target: { value: "x" } });
    await settle();

    expect(history).toHaveBeenLastCalledWith("t", null, expect.anything());
  });
});

describe("the filters", () => {
  it("offers 'only mine' just when somebody else's question is present", async () => {
    // A filter that never changes anything teaches people to ignore filters.
    render(<History token="t" onAsk={vi.fn()} />);
    await settle();

    expect(screen.queryByRole("button", { name: "Only mine" })).toBeNull();
  });

  it("offers it on a shared history", async () => {
    history.mockResolvedValue({
      entries: [entry("q1", "mine"), entry("q2", "a colleague's", false)],
      next_cursor: null,
    });
    render(<History token="t" onAsk={vi.fn()} />);
    await settle();

    expect(await screen.findByRole("button", { name: "Only mine" })).toBeTruthy();
  });

  it("asks the server rather than filtering what is already on screen", async () => {
    // Filtering the loaded page would silently exclude everything on later pages.
    render(<History token="t" onAsk={vi.fn()} />);
    await settle();

    fireEvent.click(screen.getByRole("button", { name: "Found nothing" }));
    await settle();

    await waitFor(() =>
      expect(history).toHaveBeenLastCalledWith("t", null, {
        search: "",
        mine: false,
        unanswered: true,
      }),
    );
  });

  it("shows its state without needing the results read", async () => {
    render(<History token="t" onAsk={vi.fn()} />);
    await settle();

    const chip = screen.getByRole("button", { name: "Found nothing" });
    expect(chip.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(chip);
    await settle();

    expect(screen.getByRole("button", { name: "Found nothing" }).getAttribute("aria-pressed")).toBe(
      "true",
    );
  });
});

describe("finding nothing", () => {
  it("says the filter matched nothing rather than that you have never asked", async () => {
    // Two different questions with two different answers: one is fixed by changing the
    // filter, the other by asking something.
    history.mockResolvedValue({ entries: [], next_cursor: null });
    render(<History token="t" onAsk={vi.fn()} />);
    await settle();

    expect(await screen.findByText("You have not asked anything yet.")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Search questions"), { target: { value: "zzz" } });
    await settle();

    expect(await screen.findByText("No question matches that.")).toBeTruthy();
  });
});
