/**
 * The chip, wherever a label is shown.
 *
 * Two properties beyond "it renders the name": colour is derived rather than stored, so
 * the same label must look the same everywhere and across sessions; and a document with no
 * labels gets the frontend-only `Uncategorized` pill, which stands for the empty array
 * that already means "visible tenant-wide" in the schema.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TagChip, TagChips } from "./TagChip";

describe("a namespaced label", () => {
  it("shows the whole path by default", () => {
    render(<TagChip name="finance/2026/invoices" />);

    expect(screen.getByText("finance/2026/invoices")).toBeTruthy();
  });

  it("shows only the leaf when the parent is already on screen", () => {
    // In a list already grouped under `finance/2026`, repeating it in every chip is noise.
    render(<TagChip name="finance/2026/invoices" short />);

    expect(screen.getByText("invoices")).toBeTruthy();
    expect(screen.queryByText("finance/2026/invoices")).toBeNull();
  });

  it("keeps the full path reachable when abbreviated", () => {
    // The leaf alone is ambiguous — `2026` under two namespaces is two different labels —
    // so the title carries what the text drops.
    render(<TagChip name="finance/2026/invoices" short />);

    expect(screen.getByTitle("finance/2026/invoices")).toBeTruthy();
  });
});

describe("colour", () => {
  it("is the same for the same name every time", () => {
    // Derived from the name rather than stored, which is only useful if it is stable: a
    // label that changed colour between screens would be worse than no colour at all.
    const first = render(<TagChip name="legal/contracts" />).container.querySelector("span");
    const second = render(<TagChip name="legal/contracts" />).container.querySelector("span");

    expect(first?.className).toBe(second?.className);
  });

  it("differs between a parent and its child", () => {
    // They are unrelated labels that share a prefix; giving them one colour would imply
    // an inheritance the access model does not have.
    const parent = render(<TagChip name="legal" />).container.querySelector("span");
    const child = render(<TagChip name="legal/contracts" />).container.querySelector("span");

    expect(parent?.className).not.toBe(child?.className);
  });
});

describe("clicking", () => {
  it("reports the full path, not the abbreviated label", () => {
    // The filter is applied against the real label name; sending the leaf would match the
    // wrong one, or nothing.
    const onClick = vi.fn();
    render(<TagChip name="finance/2026/invoices" short onClick={onClick} />);

    fireEvent.click(screen.getByRole("button"));

    expect(onClick).toHaveBeenCalledWith("finance/2026/invoices");
  });

  it("does not reach the row it sits in", () => {
    // A chip lives inside a clickable row — `<li>` in the document list — and clicking it
    // must filter rather than open the document. Asserted on a container that handles the
    // click, which is the real structure: the chips are siblings of the row's button, not
    // nested inside it, because a button inside a button is invalid HTML.
    const onRow = vi.fn();
    const onChip = vi.fn();
    render(
      // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
      <li onClick={onRow}>
        <TagChip name="legal" onClick={onChip} />
      </li>,
    );

    // The accessible name is the chip's text; `title` is a tooltip, not a label.
    fireEvent.click(screen.getByRole("button", { name: "legal" }));

    expect(onChip).toHaveBeenCalled();
    expect(onRow).not.toHaveBeenCalled();
  });

  it("is not a button at all when there is nothing to filter", () => {
    render(<TagChip name="legal" />);

    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("a document's chips", () => {
  it("renders every label it carries", () => {
    render(<TagChips names={["legal/contracts", "tax"]} />);

    expect(screen.getByText("legal/contracts")).toBeTruthy();
    expect(screen.getByText("tax")).toBeTruthy();
  });

  it("renders the uncategorised pill when it carries none", () => {
    // Frontend-only: the empty array already means "visible tenant-wide" in the schema,
    // and a real label for it would change that to "visible to whoever holds it".
    render(<TagChips names={[]} />);

    expect(screen.getByText("Uncategorized")).toBeTruthy();
  });

  it("does not make the uncategorised pill a filter", () => {
    // There is no label to filter by. `GET /documents` has a separate `unlabelled` flag
    // for that folder, and a chip pretending to be a label would send a name matching none.
    render(<TagChips names={[]} onSelect={vi.fn()} />);

    expect(screen.queryByRole("button")).toBeNull();
  });
});
