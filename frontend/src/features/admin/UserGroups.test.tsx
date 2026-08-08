/**
 * Putting people in groups, and the two ways that goes wrong quietly.
 *
 * Membership is access. Editing it a tick at a time would produce a run of intermediate
 * states each briefly true — out of Finance, into Legal, and for a moment in neither — so
 * ticks are local and Save sends the whole set. And a screen that congratulates itself
 * without re-reading is a screen that can be wrong about who has access, so the assertions
 * here follow the request through to what the server says afterwards.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { UserGroups } from "./UserGroups";

const GROUPS = [
  { id: "g1", name: "Finance", description: null, members: 1, label_ids: [] },
  { id: "g2", name: "Legal", description: null, members: 0, label_ids: [] },
];

const users = vi.fn();
const groups = vi.fn();
const setUserGroups = vi.fn();

vi.mock("./api", () => ({
  users: (...a: unknown[]) => users(...a),
  groups: (...a: unknown[]) => groups(...a),
  setUserGroups: (...a: unknown[]) => setUserGroups(...a),
}));

const ANA = { id: "u1", email: "ana@example.com", name: "Ana", role_ids: ["r1"], group_ids: ["g1"] };
const BEN = { id: "u2", email: "ben@example.com", name: null, role_ids: [], group_ids: [] };

beforeEach(() => {
  vi.clearAllMocks();
  users.mockResolvedValue([ANA, BEN]);
  groups.mockResolvedValue(GROUPS);
  setUserGroups.mockResolvedValue(undefined);
});

describe("the list", () => {
  it("shows everybody, by the name they chose or the address they have", async () => {
    render(<UserGroups token="t" />);

    expect(await screen.findByText("Ana")).toBeTruthy();
    expect(screen.getByText("ben@example.com")).toBeTruthy();
  });

  it("shows current membership as already ticked", async () => {
    render(<UserGroups token="t" />);

    expect((await screen.findByLabelText("Ana in Finance")).getAttribute("aria-checked")).toBe(
      "true",
    );
    expect(screen.getByLabelText("Ana in Legal").getAttribute("aria-checked")).toBe("false");
  });

  it("says plainly when somebody is in no group", async () => {
    // An empty row reads as "not configured yet". This is a statement about their access.
    render(<UserGroups token="t" />);

    expect(await screen.findByText(/In no group/)).toBeTruthy();
  });
});

describe("editing", () => {
  it("does not save on a tick", async () => {
    render(<UserGroups token="t" />);

    fireEvent.click(await screen.findByLabelText("Ana in Legal"));

    expect(setUserGroups).not.toHaveBeenCalled();
  });

  it("offers Save only once something has changed", async () => {
    render(<UserGroups token="t" />);
    await screen.findByLabelText("Ana in Finance");

    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();

    fireEvent.click(screen.getByLabelText("Ana in Legal"));

    expect(screen.getByRole("button", { name: "Save" })).toBeTruthy();
  });

  it("sends the whole set the person should be in afterwards", async () => {
    render(<UserGroups token="t" />);

    fireEvent.click(await screen.findByLabelText("Ana in Legal"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(setUserGroups).toHaveBeenCalledWith("t", "u1", ["g1", "g2"]));
  });

  it("removes a membership by sending the set without it", async () => {
    render(<UserGroups token="t" />);

    fireEvent.click(await screen.findByLabelText("Ana in Finance"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(setUserGroups).toHaveBeenCalledWith("t", "u1", []));
  });

  it("re-reads the directory rather than trusting its own copy", async () => {
    // The server decides membership. A screen that patches local state and moves on can be
    // wrong about who has access and never find out.
    render(<UserGroups token="t" />);

    fireEvent.click(await screen.findByLabelText("Ana in Legal"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(users).toHaveBeenCalledTimes(2));
  });

  it("edits one person without touching another", async () => {
    render(<UserGroups token="t" />);

    fireEvent.click(await screen.findByLabelText("Ana in Legal"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(setUserGroups).toHaveBeenCalledTimes(1));
    expect(setUserGroups).toHaveBeenCalledWith("t", "u1", expect.anything());
  });

  it("puts the ticks back when the edit is cancelled", async () => {
    render(<UserGroups token="t" />);

    fireEvent.click(await screen.findByLabelText("Ana in Legal"));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.getByLabelText("Ana in Legal").getAttribute("aria-checked")).toBe("false");
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });
});

describe("when the server refuses", () => {
  it("says so instead of leaving the screen claiming the change was made", async () => {
    setUserGroups.mockRejectedValue(new Error("no group(s): g2"));
    render(<UserGroups token="t" />);

    fireEvent.click(await screen.findByLabelText("Ana in Legal"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText("no group(s): g2")).toBeTruthy();
  });
});

describe("before any group exists", () => {
  it("points at what has to happen first rather than showing an empty row", async () => {
    // Chips with nothing in them read as a screen that failed to load, and the fix is not
    // on this panel — somebody has to create a group above it.
    groups.mockResolvedValue([]);
    render(<UserGroups token="t" />);

    expect(await screen.findByText(/No groups yet/)).toBeTruthy();
    expect(screen.queryByText("Ana")).toBeNull();
  });
});
