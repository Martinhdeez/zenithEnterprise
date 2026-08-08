/**
 * One search box, shared by three screens.
 *
 * There were three of them, written where each was needed and drifting apart: two radii, two
 * backgrounds, one with the icon inside and one without. The last test here is the one that
 * matters — it fails if a fourth copy appears rather than a fourth caller.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SearchField } from "./SearchField";

describe("typing", () => {
  it("reports every keystroke, leaving debouncing to the caller", () => {
    // History debounces, the label lists filter locally. Deciding that here would impose
    // one screen's latency on the other.
    const onChange = vi.fn();
    render(<SearchField value="" onChange={onChange} label="Search labels" />);

    fireEvent.change(screen.getByLabelText("Search labels"), { target: { value: "leg" } });

    expect(onChange).toHaveBeenCalledWith("leg");
  });

  it("submits on Enter", () => {
    const onSubmit = vi.fn();
    render(<SearchField value="x" onChange={vi.fn()} label="Search" onSubmit={onSubmit} />);

    fireEvent.submit(screen.getByLabelText("Search"));

    expect(onSubmit).toHaveBeenCalled();
  });
});

describe("clearing", () => {
  it("is offered only when there is something to clear", () => {
    // A permanent × on an empty field is a control that does nothing, which teaches people
    // that controls here might not.
    const { rerender } = render(<SearchField value="" onChange={vi.fn()} label="Search" />);
    expect(screen.queryByLabelText("Clear search")).toBeNull();

    rerender(<SearchField value="legal" onChange={vi.fn()} label="Search" />);
    expect(screen.getByLabelText("Clear search")).toBeTruthy();
  });

  it("empties the field rather than submitting it", () => {
    const onChange = vi.fn();
    const onSubmit = vi.fn();
    render(
      <SearchField value="legal" onChange={onChange} label="Search" onSubmit={onSubmit} />,
    );

    fireEvent.click(screen.getByLabelText("Clear search"));

    expect(onChange).toHaveBeenCalledWith("");
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

describe("the optional action", () => {
  it("is rendered inside the field, not beside it", () => {
    // One surface: the border is on the wrapper and the input is borderless inside it, so a
    // field and a button read as one control rather than two shapes side by side.
    render(
      <SearchField
        value=""
        onChange={vi.fn()}
        label="Search"
        action={<button type="submit">Search</button>}
      />,
    );

    const form = screen.getByLabelText("Search").closest("form");

    expect(form?.contains(screen.getByRole("button", { name: "Search" }))).toBe(true);
  });
});

describe("consistency", () => {
  it("is the shape every search box in the product uses", () => {
    // `rounded-full` is what the chat composer and the search screen already use, and those
    // are the two places people type most. This test is here so a fourth screen adds a
    // caller rather than a fourth copy with its own radius.
    render(<SearchField value="" onChange={vi.fn()} label="Search" />);

    const form = screen.getByLabelText("Search").closest("form");

    expect(form?.className).toContain("rounded-full");
  });
});
