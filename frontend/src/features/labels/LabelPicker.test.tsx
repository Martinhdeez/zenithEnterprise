/**
 * The picker's contract with the server, which is the part that has to be right at scale.
 *
 * Nothing here checks that a filter *looks* pressed. What matters is that narrowing the
 * list is a request — with thousands of labels the client never holds the full set, so a
 * filter implemented in the browser would only ever filter the page it happened to have,
 * and would look correct while quietly hiding matches. So these assert the query string.
 *
 * The one exception is "Selected", which is deliberately local: the client already knows
 * what it ticked, and a round trip to learn it would be waste.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Label } from "./api";
import { LabelPicker } from "./LabelPicker";

const ITEMS = [
  { id: "a", name: "Contratos", is_default: false, documents: 4, last_used: "2026-08-01T00:00:00Z" },
  { id: "b", name: "Facturas", is_default: false, documents: 0, last_used: null },
];

function server() {
  const urls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      urls.push(url);
      if (url.includes("/labels/search")) {
        return new Response(JSON.stringify({ items: ITEMS, next_cursor: null }), { status: 200 });
      }
      return new Response(JSON.stringify(ITEMS[0]), { status: 201 });
    }),
  );
  return urls;
}

function picker(selected: string[] = [], known: Label[] = []) {
  const onToggle = vi.fn();
  render(
    <LabelPicker
      token="t"
      selected={new Set(selected)}
      known={new Map(known.map((label) => [label.id, label]))}
      onToggle={onToggle}
      onCreated={vi.fn()}
      onRemoved={vi.fn()}
    />,
  );
  return onToggle;
}

/** The last search request, which is the one reflecting the control just clicked. */
const lastSearch = (urls: string[]) => [...urls].reverse().find((url) => url.includes("/search"));

beforeEach(() => {
  vi.unstubAllGlobals();
  // jsdom implements no layout, so it has no `scrollIntoView` — Radix's `Select` calls it
  // when the listbox opens.
  Element.prototype.scrollIntoView = vi.fn();
});

describe("searching", () => {
  it("asks the server rather than filtering what it already has", async () => {
    const urls = server();
    picker();
    await screen.findByRole("button", { name: "Contratos" });

    fireEvent.change(screen.getByRole("textbox", { name: /search labels/i }), {
      target: { value: "contra" },
    });

    await waitFor(() => expect(lastSearch(urls)).toContain("q=contra"));
  });

  it("defaults to the most recently used, not alphabetical", async () => {
    // At this scale the label you want is nearly always one you have used before, and
    // alphabetical order buries it under thousands you have not.
    const urls = server();
    picker();

    await waitFor(() => expect(lastSearch(urls)).toContain("sort=last_used"));
  });
});

describe("the filters that have to be server-side", () => {
  it("sends in_use", async () => {
    const urls = server();
    picker();
    await screen.findByRole("button", { name: "Contratos" });

    fireEvent.click(screen.getByRole("button", { name: /in use/i }));

    await waitFor(() => expect(lastSearch(urls)).toContain("in_use=true"));
  });

  it("sends mine", async () => {
    const urls = server();
    picker();
    await screen.findByRole("button", { name: "Contratos" });

    fireEvent.click(screen.getByRole("button", { name: /^mine$/i }));

    await waitFor(() => expect(lastSearch(urls)).toContain("mine=true"));
  });

  it("combines them rather than replacing one with the other", async () => {
    const urls = server();
    picker();
    await screen.findByRole("button", { name: "Contratos" });

    fireEvent.click(screen.getByRole("button", { name: /in use/i }));
    fireEvent.click(screen.getByRole("button", { name: /^mine$/i }));

    await waitFor(() => {
      const url = lastSearch(urls) ?? "";
      expect(url).toContain("in_use=true");
      expect(url).toContain("mine=true");
    });
  });
});

describe("the filter that is deliberately local", () => {
  it("shows the selection without asking the server", async () => {
    const known = [{ id: "z", name: "Elegida", is_default: false }];
    const urls = server();
    picker(["z"], known);
    await screen.findByRole("button", { name: "Contratos" });
    const before = urls.filter((url) => url.includes("/search")).length;

    fireEvent.click(screen.getByRole("button", { name: /selected/i }));

    expect(await screen.findByRole("button", { name: "Elegida" })).toBeTruthy();
    // The list narrows to the ticked labels and the search results go away — with no new
    // request, because the client already knew.
    expect(screen.queryByRole("button", { name: "Contratos" })).toBeNull();
    expect(urls.filter((url) => url.includes("/search")).length).toBe(before);
  });

  it("is unavailable when nothing is ticked", async () => {
    server();
    picker();
    await screen.findByRole("button", { name: "Contratos" });

    expect(screen.getByRole("button", { name: /selected/i }).hasAttribute("disabled")).toBe(true);
  });
});

describe("what the parent is told", () => {
  it("reports the whole label, not just its id", async () => {
    // The parent renders the selected chips, and a label ticked out of a search result may
    // be one it has never seen — without the name it would have nothing to draw.
    server();
    const onToggle = picker();

    fireEvent.click(await screen.findByRole("button", { name: "Contratos" }));

    expect(onToggle).toHaveBeenCalledWith(expect.objectContaining({ id: "a", name: "Contratos" }));
  });

  it("keeps a ticked label visible even when the search no longer contains it", async () => {
    // `known` is what makes this possible: the selected row is drawn from the parent's map
    // rather than from the current page of results.
    server();
    picker(["z"], [{ id: "z", name: "Fuera de la búsqueda", is_default: false }]);

    expect(
      await screen.findByRole("button", { name: /remove fuera de la búsqueda/i }),
    ).toBeTruthy();
  });
});

describe("the tenant-wide warning", () => {
  it("says so when nothing is selected", async () => {
    // An unlabelled document is visible to the whole tenant — chosen deliberately in
    // mvp.md 2.2, and the one consequence of leaving this form alone nobody would guess.
    server();
    picker();

    expect(await screen.findByText(/visible tenant-wide/i)).toBeTruthy();
  });
});
