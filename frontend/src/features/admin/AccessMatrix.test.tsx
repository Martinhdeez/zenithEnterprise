/**
 * Mapping labels to a group, one group at a time.
 *
 * This was a grid — groups down the side, labels across the top — and the shape was wrong:
 * a matrix grows in two directions and only one of these axes is bounded, so it got wider
 * than the screen and stayed there. The assertions below are about the shape that replaced
 * it: one dimension at a time, a search over the unbounded one, and a Save that means
 * something because ticking no longer writes.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AccessMatrix } from "./AccessMatrix";

const LABELS = [
  { id: "l1", name: "hr/payroll", is_default: false, priority_level: 8 },
  { id: "l2", name: "finance/routine", is_default: false, priority_level: 0 },
  { id: "l3", name: "legal/contracts", is_default: false, priority_level: 2 },
];

const GROUPS = [
  { id: "g1", name: "Human Resources", description: null, members: 3, label_ids: ["l1"] },
  { id: "g2", name: "Engineering", description: null, members: 12, label_ids: [] },
];

const groups = vi.fn();
const setGroupLabels = vi.fn();
const setLabelClearance = vi.fn();

vi.mock("./api", () => ({
  groups: (...a: unknown[]) => groups(...a),
  setGroupLabels: (...a: unknown[]) => setGroupLabels(...a),
  setLabelClearance: (...a: unknown[]) => setLabelClearance(...a),
  createGroup: vi.fn(),
  deleteGroup: vi.fn(),
}));

beforeEach(() => {
  vi.clearAllMocks();
  groups.mockResolvedValue(GROUPS);
  setGroupLabels.mockImplementation((_t: string, id: string, labelIds: string[]) =>
    Promise.resolve({ ...GROUPS.find((g) => g.id === id)!, label_ids: labelIds }),
  );
  setLabelClearance.mockResolvedValue({ id: "l1", name: "hr/payroll", priority_level: 5 });
});

describe("choosing a group", () => {
  it("starts on the first one rather than on nothing", async () => {
    // An empty right-hand pane on load is a screen that looks broken.
    render(<AccessMatrix token="t" labels={LABELS} />);

    expect(await screen.findByLabelText("Human Resources may reach hr/payroll")).toBeTruthy();
  });

  it("says how much each group holds without opening it", async () => {
    render(<AccessMatrix token="t" labels={LABELS} />);

    expect(await screen.findByText("1 label · 3 members")).toBeTruthy();
    expect(screen.getByText("0 labels · 12 members")).toBeTruthy();
  });

  it("shows the labels of whichever group is open", async () => {
    render(<AccessMatrix token="t" labels={LABELS} />);
    fireEvent.click(await screen.findByRole("button", { name: /Engineering/ }));

    const mapped = screen.getByLabelText("Engineering may reach hr/payroll");

    expect(mapped.getAttribute("aria-checked")).toBe("false");
  });
});

describe("searching the labels", () => {
  it("narrows the list, because thousands of them is the real case", async () => {
    render(<AccessMatrix token="t" labels={LABELS} />);
    await screen.findByLabelText("Human Resources may reach hr/payroll");

    fireEvent.change(screen.getByLabelText("Search labels to map"), { target: { value: "legal" } });

    expect(screen.getByText("legal/contracts")).toBeTruthy();
    expect(screen.queryByText("finance/routine")).toBeNull();
  });

  it("says so when nothing matches", async () => {
    render(<AccessMatrix token="t" labels={LABELS} />);
    await screen.findByLabelText("Human Resources may reach hr/payroll");

    fireEvent.change(screen.getByLabelText("Search labels to map"), { target: { value: "zzz" } });

    expect(screen.getByText("No label matches that.")).toBeTruthy();
  });

  it("puts what the group already opens at the top", async () => {
    // The question this screen answers most often is "what does this group open", and that
    // answer should not be somewhere down an alphabetical list of everything else.
    render(<AccessMatrix token="t" labels={LABELS} />);
    await screen.findByLabelText("Human Resources may reach hr/payroll");

    const names = screen.getAllByRole("checkbox").map((row) => row.getAttribute("aria-label"));

    expect(names[0]).toContain("hr/payroll");
  });
});

describe("saving", () => {
  it("does not write on a tick", async () => {
    // It used to, and that read as nothing having happened: there was no moment where the
    // screen said "this is now true".
    render(<AccessMatrix token="t" labels={LABELS} />);
    fireEvent.click(await screen.findByLabelText("Human Resources may reach finance/routine"));

    expect(setGroupLabels).not.toHaveBeenCalled();
  });

  it("offers Save only once something has changed", async () => {
    render(<AccessMatrix token="t" labels={LABELS} />);
    await screen.findByLabelText("Human Resources may reach hr/payroll");

    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();

    fireEvent.click(screen.getByLabelText("Human Resources may reach finance/routine"));

    expect(screen.getByRole("button", { name: "Save" })).toBeTruthy();
  });

  it("sends the whole set the group should open afterwards", async () => {
    // Replace, not patch: the server takes the complete set, and a delta computed from
    // stale state would silently revoke a mapping somebody else had just made.
    render(<AccessMatrix token="t" labels={LABELS} />);
    fireEvent.click(await screen.findByLabelText("Human Resources may reach finance/routine"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(setGroupLabels).toHaveBeenCalledWith("t", "g1", expect.arrayContaining(["l1", "l2"])),
    );
  });

  it("puts the ticks back when the edit is cancelled", async () => {
    render(<AccessMatrix token="t" labels={LABELS} />);
    fireEvent.click(await screen.findByLabelText("Human Resources may reach finance/routine"));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    const row = screen.getByLabelText("Human Resources may reach finance/routine");

    expect(row.getAttribute("aria-checked")).toBe("false");
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("discards unsaved ticks when another group is opened", async () => {
    // They belong to the group they were made on. Carrying them across would apply
    // somebody's intent to the wrong group.
    render(<AccessMatrix token="t" labels={LABELS} />);
    fireEvent.click(await screen.findByLabelText("Human Resources may reach finance/routine"));
    fireEvent.click(screen.getByRole("button", { name: /Engineering/ }));

    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("reports a refusal instead of showing the change as made", async () => {
    setGroupLabels.mockRejectedValue(new Error("no label(s): l2"));
    render(<AccessMatrix token="t" labels={LABELS} />);
    fireEvent.click(await screen.findByLabelText("Human Resources may reach finance/routine"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toBeTruthy();
  });
});

describe("clearance", () => {
  it("sits beside each label rather than in a column header", async () => {
    render(<AccessMatrix token="t" labels={LABELS} />);

    const control = (await screen.findByLabelText(
      "Clearance required by hr/payroll",
    )) as HTMLSelectElement;

    expect(control.value).toBe("8");
  });

  it("distinguishes demanding no clearance from being public", async () => {
    render(<AccessMatrix token="t" labels={LABELS} />);
    await screen.findByLabelText("Search labels to map");

    const control = (await screen.findByLabelText(
      "Clearance required by finance/routine",
    )) as HTMLSelectElement;

    expect(control.value).toBe("0");
    expect(screen.getAllByText("no clearance").length).toBe(LABELS.length);
  });

  it("saves immediately, unlike a tick", async () => {
    // A clearance belongs to the *label*, not to this group's mapping: raising it changes
    // what every group opens. Putting a change that wide behind a button labelled for one
    // group would imply a scope it does not have.
    render(<AccessMatrix token="t" labels={LABELS} />);
    fireEvent.change(await screen.findByLabelText("Clearance required by hr/payroll"), {
      target: { value: "5" },
    });

    await waitFor(() => expect(setLabelClearance).toHaveBeenCalledWith("t", "l1", 5));
  });

  it("says that it saved, since no button is going to", async () => {
    // Without a mark, a control that writes on change is indistinguishable from one that
    // does nothing — which is exactly what the tick boxes looked like before they grew a
    // Save button, and what prompted this.
    render(<AccessMatrix token="t" labels={LABELS} />);
    fireEvent.change(await screen.findByLabelText("Clearance required by hr/payroll"), {
      target: { value: "5" },
    });

    expect((await screen.findByRole("status")).textContent).toBe("saved");
  });

  it("does not offer Save for a clearance change", async () => {
    // The question this answers: a level change and a tick sit on the same row and behave
    // differently, and the difference is that one of them is not about this group at all.
    render(<AccessMatrix token="t" labels={LABELS} />);
    fireEvent.change(await screen.findByLabelText("Clearance required by hr/payroll"), {
      target: { value: "5" },
    });

    await waitFor(() => expect(setLabelClearance).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("explains which control is which", async () => {
    render(<AccessMatrix token="t" labels={LABELS} />);

    expect(
      await screen.findByText(/applies to that label everywhere and saves straight away/i),
    ).toBeTruthy();
  });

  it("states that the two halves are independent", async () => {
    render(<AccessMatrix token="t" labels={LABELS} />);

    expect(await screen.findByText(/the two are independent/i)).toBeTruthy();
  });
});


describe("the row itself", () => {
  it("is the control, not a checkbox beside it", async () => {
    // A native `<input type="checkbox">` is drawn by the operating system and cannot be
    // made to match anything else on the page — and it puts a 16-pixel target beside a row
    // four hundred wide.
    render(<AccessMatrix token="t" labels={LABELS} />);
    const row = await screen.findByLabelText("Human Resources may reach finance/routine");

    expect(row.tagName).toBe("BUTTON");
    expect(row.getAttribute("role")).toBe("checkbox");
  });

  it("toggles when the label text is clicked", async () => {
    render(<AccessMatrix token="t" labels={LABELS} />);
    await screen.findByLabelText("Human Resources may reach finance/routine");

    fireEvent.click(screen.getByText("finance/routine"));

    expect(screen.getByRole("button", { name: "Save" })).toBeTruthy();
  });

  it("keeps the clearance control out of the toggle", async () => {
    // A control nested inside a control is invalid HTML and unreachable by keyboard, and
    // changing a level must not also tick the row.
    render(<AccessMatrix token="t" labels={LABELS} />);
    const row = await screen.findByLabelText("Human Resources may reach hr/payroll");

    expect(row.querySelector("select")).toBeNull();

    fireEvent.change(screen.getByLabelText("Clearance required by hr/payroll"), {
      target: { value: "5" },
    });

    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });
});
