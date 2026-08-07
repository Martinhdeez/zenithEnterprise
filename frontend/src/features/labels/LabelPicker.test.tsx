/**
 * The label picker's one structural decision: which shape it renders.
 *
 * Below the threshold every label is a chip, visible without typing. Above it the same
 * selection becomes a searchable popover. Both drive the identical `onToggle` contract, so
 * what these tests hold is that switching presentation never changes what the parent
 * receives — the bug this would otherwise hide is an upload that silently files a document
 * under nothing because the picker changed shape at 25 labels.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Label } from "./api";
import { COMBOBOX_THRESHOLD, LabelPicker } from "./LabelPicker";

const labels = (count: number): Label[] =>
  Array.from({ length: count }, (_, index) => ({
    id: `id-${index}`,
    name: `Label ${index}`,
    is_default: index === 0,
  }));

function picker(available: Label[], selected: string[] = []) {
  const onToggle = vi.fn();
  render(
    <LabelPicker
      token="t"
      available={available}
      selected={new Set(selected)}
      onToggle={onToggle}
      onCreated={vi.fn()}
      onRemoved={vi.fn()}
    />,
  );
  return onToggle;
}

describe("which shape it renders", () => {
  it("shows every label as a chip below the threshold", () => {
    picker(labels(3));

    expect(screen.getByRole("button", { name: "Label 0" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Label 2" })).toBeTruthy();
    // The chip grid's inline creation, which the combobox replaces with its own.
    expect(screen.getByRole("button", { name: /new label/i })).toBeTruthy();
  });

  it("switches to a searchable combobox above the threshold", () => {
    picker(labels(COMBOBOX_THRESHOLD + 1));

    // A closed popover renders no options at all, which is the whole point: the wall of
    // chips is gone and nothing is visible until it is searched.
    expect(screen.getByRole("combobox")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Label 0" })).toBeNull();
  });

  it("keeps the chip grid exactly at the threshold", () => {
    // The boundary, asserted because `>` and `>=` are one character apart and the
    // difference is invisible in every test that uses a round number.
    picker(labels(COMBOBOX_THRESHOLD));

    expect(screen.getByRole("button", { name: "Label 0" })).toBeTruthy();
    expect(screen.queryByRole("combobox")).toBeNull();
  });
});

describe("what the parent receives", () => {
  it("reports a chip click as a toggle", () => {
    const onToggle = picker(labels(3));

    fireEvent.click(screen.getByRole("button", { name: "Label 1" }));

    expect(onToggle).toHaveBeenCalledWith("id-1");
  });

  it("shows what is selected above the combobox, and unselects it on click", () => {
    // Past the threshold the chosen labels are the only ones on screen when the popover is
    // shut — without them the form would give no indication of what it is about to file
    // the document under.
    const onToggle = picker(labels(COMBOBOX_THRESHOLD + 1), ["id-2"]);

    fireEvent.click(screen.getByRole("button", { name: /remove label 2/i }));

    expect(onToggle).toHaveBeenCalledWith("id-2");
  });

  it("counts the selection on the trigger", () => {
    picker(labels(COMBOBOX_THRESHOLD + 1), ["id-1", "id-2"]);

    expect(screen.getByRole("combobox").textContent).toContain("2 selected");
  });
});

describe("the tenant-wide warning", () => {
  it("says so when nothing is selected, in either shape", () => {
    const { unmount } = render(
      <LabelPicker
        token="t"
        available={labels(3)}
        selected={new Set()}
        onToggle={vi.fn()}
        onCreated={vi.fn()}
        onRemoved={vi.fn()}
      />,
    );
    // An unlabelled document is visible to the whole tenant — chosen deliberately in
    // mvp.md 2.2, and the one consequence of leaving this form alone that a user would not
    // otherwise guess.
    expect(screen.getByText(/visible tenant-wide/i)).toBeTruthy();
    unmount();

    picker(labels(COMBOBOX_THRESHOLD + 1));
    expect(screen.getByText(/visible tenant-wide/i)).toBeTruthy();
  });
});
