/**
 * The grid, and the sentence it must not let anybody misread.
 *
 * A ticked cell maps a label to a group. It does *not* mean the group's members can read
 * that label — they still need the clearance the label demands. The screen is the only
 * place that distinction is visible before somebody files a support ticket about it, so
 * the clearance control and the explanation are asserted here rather than treated as
 * decoration.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AccessMatrix } from "./AccessMatrix";

const LABELS = [
  { id: "l1", name: "hr/payroll", is_default: false, priority_level: 8 },
  { id: "l2", name: "finance/routine", is_default: false, priority_level: 0 },
];

const GROUPS = [
  { id: "g1", name: "Human Resources", description: null, members: 3, label_ids: ["l1"] },
  { id: "g2", name: "Engineering", description: null, members: 12, label_ids: [] },
];

const setGroupLabels = vi.fn();
const setLabelClearance = vi.fn();

vi.mock("./api", () => ({
  groups: () => Promise.resolve(GROUPS),
  setGroupLabels: (...args: unknown[]) => setGroupLabels(...args),
  setLabelClearance: (...args: unknown[]) => setLabelClearance(...args),
  createGroup: vi.fn(),
  deleteGroup: vi.fn(),
}));

beforeEach(() => {
  vi.clearAllMocks();
  setGroupLabels.mockImplementation((_t, id, labelIds) =>
    Promise.resolve({ ...GROUPS.find((g) => g.id === id)!, label_ids: labelIds }),
  );
  setLabelClearance.mockResolvedValue({ id: "l1", name: "hr/payroll", priority_level: 5 });
});

describe("the grid", () => {
  it("puts every group against every label", async () => {
    render(<AccessMatrix token="t" labels={LABELS} />);

    expect(await screen.findByText("Human Resources")).toBeTruthy();
    expect(screen.getByText("Engineering")).toBeTruthy();
    expect(screen.getByText("hr/payroll")).toBeTruthy();
    expect(screen.getByText("finance/routine")).toBeTruthy();
  });

  it("shows an existing mapping as ticked and an absent one as not", async () => {
    render(<AccessMatrix token="t" labels={LABELS} />);

    const mapped = await screen.findByLabelText("Human Resources may reach hr/payroll");
    const unmapped = screen.getByLabelText("Engineering may reach hr/payroll");

    expect((mapped as HTMLInputElement).checked).toBe(true);
    expect((unmapped as HTMLInputElement).checked).toBe(false);
  });

  it("says how many people a group is", async () => {
    // The number that makes a revocation reviewable before it is made.
    render(<AccessMatrix token="t" labels={LABELS} />);

    expect(await screen.findByText("3 members")).toBeTruthy();
  });
});

describe("changing a mapping", () => {
  it("sends the whole set the group should have afterwards", async () => {
    // Replace, not patch: the server takes the complete set, and a delta computed from
    // stale state would silently revoke a mapping somebody else had just made.
    render(<AccessMatrix token="t" labels={LABELS} />);

    fireEvent.click(await screen.findByLabelText("Engineering may reach hr/payroll"));

    await waitFor(() => expect(setGroupLabels).toHaveBeenCalledWith("t", "g2", ["l1"]));
  });

  it("removes a label by sending the set without it", async () => {
    render(<AccessMatrix token="t" labels={LABELS} />);

    fireEvent.click(await screen.findByLabelText("Human Resources may reach hr/payroll"));

    await waitFor(() => expect(setGroupLabels).toHaveBeenCalledWith("t", "g1", []));
  });

  it("reports a refusal instead of leaving the tick where the server did not put it", async () => {
    setGroupLabels.mockRejectedValue(new Error("no label(s): l1"));
    render(<AccessMatrix token="t" labels={LABELS} />);

    fireEvent.click(await screen.findByLabelText("Engineering may reach hr/payroll"));

    expect(await screen.findByText("no label(s): l1")).toBeTruthy();
    expect(
      (screen.getByLabelText("Engineering may reach hr/payroll") as HTMLInputElement).checked,
    ).toBe(false);
  });
});

describe("clearance", () => {
  it("is shown per label, because a tick alone does not grant access", async () => {
    render(<AccessMatrix token="t" labels={LABELS} />);

    const control = (await screen.findByLabelText(
      "Clearance required by hr/payroll",
    )) as HTMLSelectElement;

    expect(control.value).toBe("8");
  });

  it("distinguishes demanding no clearance from being public", async () => {
    // `finance/routine` is at 0 — it asks for no clearance, which is not the same as being
    // readable by anyone: a label mapped to no group is still reachable by nobody.
    render(<AccessMatrix token="t" labels={LABELS} />);

    const control = (await screen.findByLabelText(
      "Clearance required by finance/routine",
    )) as HTMLSelectElement;

    expect(control.value).toBe("0");
    // Worded rather than shown as "0": a bare zero in a column header reads as "no
    // restriction at all", which is the misreading this whole screen exists to prevent.
    expect(screen.getAllByText("no clearance").length).toBe(LABELS.length);
  });

  it("classifies a label in place", async () => {
    render(<AccessMatrix token="t" labels={LABELS} />);

    fireEvent.change(await screen.findByLabelText("Clearance required by hr/payroll"), {
      target: { value: "5" },
    });

    await waitFor(() => expect(setLabelClearance).toHaveBeenCalledWith("t", "l1", 5));
  });

  it("states that the two halves are independent", async () => {
    // The one sentence that stops an administrator reading a ticked row as "these people
    // can see this".
    render(<AccessMatrix token="t" labels={LABELS} />);

    expect(await screen.findByText(/clearance is at or above/i)).toBeTruthy();
  });
});
