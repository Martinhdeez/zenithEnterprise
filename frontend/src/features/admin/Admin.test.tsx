/** Group writes must refresh both access editors on the same administration screen. */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import { Admin } from "./Admin";
import type { Group } from "./api";

const catalog = vi.hoisted(() => ({ groups: [] as Group[] }));
vi.mock("./api", () => ({
  groups: vi.fn(async () => catalog.groups),
  users: vi.fn(async () => [{ id: "u1", email: "ana@example.com", name: "Ana", role_ids: [], group_ids: [] }]),
  createGroup: vi.fn(async (_token: string, input: { name: string }) => {
    const group = { id: "g1", name: input.name, description: null, members: 0, label_ids: [] };
    catalog.groups = [group];
    return group;
  }),
  deleteGroup: vi.fn(async () => { catalog.groups = []; }),
}));
vi.mock("@/features/labels", () => ({
  labels: vi.fn(async () => [{ id: "l1", name: "Public", is_default: false, priority_level: 0 }]),
  TagManager: () => null,
}));
vi.mock("./analytics/Analytics", () => ({ Analytics: () => null }));
vi.mock("./audit/AuditTrail", () => ({ AuditTrail: () => null }));
vi.mock("./invite/InvitePanel", () => ({ InvitePanel: () => null }));
vi.mock("./model/LlmPanel", () => ({ LlmPanel: () => null }));
vi.mock("./roles/RolePanel", () => ({ RolePanel: () => null }));

beforeEach(() => {
  catalog.groups = [];
  vi.clearAllMocks();
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

it("creates and removes the first group across all three panels without navigation", async () => {
  render(<Admin token="t" />);
  await waitFor(() => expect(screen.getAllByText(/No groups yet/)).toHaveLength(2));

  fireEvent.change(screen.getByLabelText("New group name"), { target: { value: "Finance" } });
  fireEvent.click(screen.getByRole("button", { name: "Add group" }));

  expect(await screen.findByRole("tab", { name: /Finance/ })).toBeTruthy();
  expect(await screen.findByLabelText("Ana in Finance")).toBeTruthy();
  expect(screen.getByLabelText("Finance may reach Public")).toBeTruthy();
  expect(screen.queryByText(/No groups yet/)).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  await waitFor(() => expect(screen.getAllByText(/No groups yet/)).toHaveLength(2));
  expect(screen.queryByRole("tab", { name: /Finance/ })).toBeNull();
  expect(screen.queryByLabelText("Ana in Finance")).toBeNull();
});
