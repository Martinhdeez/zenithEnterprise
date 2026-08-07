/**
 * The merge confirmation, which is the only part of this screen that is a safeguard rather
 * than a table.
 *
 * Labels are the access-control primitive, so folding two together moves documents between
 * roles. The server computes that as `visibility_widening` and refuses to do it
 * unacknowledged; what these tests hold is the client half of that contract — that the
 * number is fetched and displayed *before* any request that changes anything, and that
 * nothing in this dialog can commit a merge whose consequences were never on screen.
 *
 * A regression here would not fail loudly. It would be a merge dialog that quietly went
 * straight to committing, which looks identical until someone audits who can read what.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { TagManager } from "./TagManager";

interface Call {
  url: string;
  body: Record<string, unknown> | null;
}

function server(overrides: { widening?: number; relabelled?: number } = {}) {
  const calls: Call[] = [];
  const items = [
    { id: "a", name: "Finance", is_default: false, documents: 4 },
    { id: "b", name: "Finanace", is_default: false, documents: 1 },
  ];

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });

      if (url.includes("/labels/search")) {
        return new Response(JSON.stringify({ items, next_cursor: null }), { status: 200 });
      }
      return new Response(
        JSON.stringify({
          target: items[0],
          merged: ["b"],
          documents_relabelled: overrides.relabelled ?? 1,
          visibility_widening: overrides.widening ?? 0,
          dry_run: JSON.parse(String(init?.body)).dry_run,
        }),
        { status: 200 },
      );
    }),
  );
  return calls;
}

async function openMergeDialog() {
  render(<TagManager token="t" />);
  await screen.findByText("Finance");

  fireEvent.click(screen.getByRole("checkbox", { name: /select finance$/i }));
  fireEvent.click(screen.getByRole("checkbox", { name: /select finanace/i }));
  fireEvent.click(screen.getByRole("button", { name: /^merge/i }));

  await screen.findByRole("dialog");
}

beforeEach(() => {
  vi.unstubAllGlobals();
  // jsdom implements no layout, so it has no `scrollIntoView` — Radix's `Select` calls it
  // when the listbox opens. Absent this the dialog's "Keep" dropdown throws on open, which
  // is a gap in the test environment rather than anything the browser would do.
  Element.prototype.scrollIntoView = vi.fn();
});

describe("the two-step merge", () => {
  it("previews with dry_run before offering to commit", async () => {
    const calls = server({ widening: 3 });
    await openMergeDialog();

    // Step one is the only button available. There is no confirm to click yet, which is
    // the structural half of the guarantee — not a disabled button someone can enable.
    expect(screen.queryByRole("button", { name: /^merge and widen/i })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /preview changes/i }));

    await waitFor(() => expect(calls.some((call) => call.url.includes("/labels/merge"))).toBe(true));
    const preview = calls.find((call) => call.url.includes("/labels/merge"));
    expect(preview?.body).toMatchObject({ dry_run: true, acknowledge_widening: false });
  });

  it("shows how many documents change hands before anything is committed", async () => {
    server({ widening: 3 });
    await openMergeDialog();

    fireEvent.click(screen.getByRole("button", { name: /preview changes/i }));

    // The number the whole dialog exists to put in front of somebody.
    expect(await screen.findByText(/3 documents become visible to roles/i)).toBeTruthy();
  });

  it("sends acknowledge_widening only on the second step", async () => {
    const calls = server({ widening: 3 });
    await openMergeDialog();

    fireEvent.click(screen.getByRole("button", { name: /preview changes/i }));
    fireEvent.click(await screen.findByRole("button", { name: /merge and widen access/i }));

    await waitFor(() => {
      const merges = calls.filter((call) => call.url.includes("/labels/merge"));
      expect(merges).toHaveLength(2);
      expect(merges[1]?.body).toMatchObject({ dry_run: false, acknowledge_widening: true });
    });
  });

  it("says plainly when a merge exposes nothing", async () => {
    // The count has to be able to say "safe", or nobody reads it when it says otherwise.
    server({ widening: 0 });
    await openMergeDialog();

    fireEvent.click(screen.getByRole("button", { name: /preview changes/i }));

    expect(await screen.findByText(/no document changes hands/i)).toBeTruthy();
    // And the commit button drops the alarm styling and the warning verb.
    expect(screen.getByRole("button", { name: /^merge$/i })).toBeTruthy();
  });

  it("discards the preview when the target changes", async () => {
    // A different target is a different merge — different documents move, and a different
    // set of roles gains them. Leaving the old preview up would let someone confirm one
    // merge while reading the numbers for another.
    server({ widening: 3 });
    await openMergeDialog();

    fireEvent.click(screen.getByRole("button", { name: /preview changes/i }));
    await screen.findByText(/3 documents become visible/i);

    fireEvent.click(screen.getByRole("combobox", { name: /keep/i }));
    fireEvent.click(await screen.findByRole("option", { name: "Finanace" }));

    await waitFor(() => {
      expect(screen.queryByText(/3 documents become visible/i)).toBeNull();
      expect(screen.getByRole("button", { name: /preview changes/i })).toBeTruthy();
    });
  });
});

describe("deleting a label", () => {
  it("surfaces the server's refusal rather than a generic failure", async () => {
    // The server refuses while documents still carry it, because deleting the last label
    // off a document leaves it visible to the whole tenant. Its wording says how many —
    // the only part the administrator can act on.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes("/labels/search")) {
          return new Response(
            JSON.stringify({
              items: [{ id: "a", name: "Finance", is_default: false, documents: 4 }],
              next_cursor: null,
            }),
            { status: 200 },
          );
        }
        return new Response(
          JSON.stringify({
            type: "https://zenith.enterprise/problems/conflict",
            title: "Conflict",
            status: 409,
            detail: "4 document(s) still carry 'Finance'. Remove it from them first.",
          }),
          { status: 409, headers: { "Content-Type": "application/problem+json" } },
        );
      }),
    );

    render(<TagManager token="t" />);
    fireEvent.click(await screen.findByRole("button", { name: /delete finance/i }));

    // `.textContent` rather than a `jest-dom` matcher: this project configures none, and
    // the rest of the suite asserts the same way.
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/4 document\(s\) still carry/i);
  });
});
