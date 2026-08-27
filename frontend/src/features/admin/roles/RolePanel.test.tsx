/**
 * A screen that offers an action the server will reject.
 *
 * The API refuses to edit a system role whatever this panel sends — `admin` and `member` are
 * `is_system`, and that is enforced in the database. Disabling the control here is not belt
 * and braces: the refusal is meant to be the safety net rather than the interface, and an
 * administrator who is allowed to click something that then fails learns that the product is
 * unreliable rather than that the role is protected.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ApiError } from "@/shared/api/http";

const roles = vi.fn();
const setRolePermissions = vi.fn();
const createRole = vi.fn();
const deleteRole = vi.fn();
const setRoleClearance = vi.fn();

vi.mock("../api", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("../api");
  return {
    ...actual,
    roles: (...args: unknown[]) => roles(...args),
    setRolePermissions: (...args: unknown[]) => setRolePermissions(...args),
    createRole: (...args: unknown[]) => createRole(...args),
    deleteRole: (...args: unknown[]) => deleteRole(...args),
    setRoleClearance: (...args: unknown[]) => setRoleClearance(...args),
  };
});

const { RolePanel } = await import("./RolePanel");

const role = (overrides: Record<string, unknown> = {}) => ({
  id: "r1",
  name: "editor",
  is_system: false,
  permissions: ["query.execute"],
  priority_level: 0,
  ...overrides,
});

/** Every permission toggle drawn for the role on screen. */
const toggles = () =>
  screen.getAllByRole("button").filter((button) => /^[a-z_]+\.[a-z.]+$/.test(button.textContent ?? ""));

beforeEach(() => {
  roles.mockReset();
  setRolePermissions.mockReset();
  createRole.mockReset();
  deleteRole.mockReset();
  setRoleClearance.mockReset();
});

describe("a system role", () => {
  it("cannot have its permissions changed from here", async () => {
    roles.mockResolvedValue([role({ id: "sys", name: "admin", is_system: true })]);

    render(<RolePanel token="t" />);
    await screen.findByText("admin");

    const controls = toggles();
    expect(controls.length).toBeGreaterThan(0);
    for (const control of controls) {
      expect((control as HTMLButtonElement).disabled).toBe(true);
    }
  });

  it("says why rather than being silently inert", async () => {
    // A disabled control with no explanation reads as a bug. The panel states the reason
    // next to the role.
    roles.mockResolvedValue([role({ id: "sys", name: "admin", is_system: true })]);

    render(<RolePanel token="t" />);

    expect(await screen.findByText(/not editable/i)).toBeTruthy();
  });
});

describe("an ordinary role", () => {
  it("can have a permission toggled", async () => {
    roles.mockResolvedValue([role()]);
    setRolePermissions.mockResolvedValue(role({ permissions: [] }));

    render(<RolePanel token="t" />);
    await screen.findByText("editor");

    const control = toggles()[0]!;
    expect((control as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(control);

    await waitFor(() => expect(setRolePermissions).toHaveBeenCalled());
  });

  it("sends the whole set rather than the one that changed", async () => {
    // The endpoint replaces the role's permissions; sending only the clicked one would
    // silently remove every other permission the role holds.
    roles.mockResolvedValue([role({ permissions: ["query.execute", "documents.upload"] })]);
    setRolePermissions.mockResolvedValue(role({ permissions: ["documents.upload"] }));

    render(<RolePanel token="t" />);
    await screen.findByText("editor");
    fireEvent.click(screen.getByRole("button", { name: "query.execute" }));

    await waitFor(() => expect(setRolePermissions).toHaveBeenCalled());
    const sent = setRolePermissions.mock.calls[0]?.[2] as string[];
    expect(sent).toContain("documents.upload");
    expect(sent).not.toContain("query.execute");
  });
});


/**
 * "A custom role is configuration rather than a release" is a sentence this product sells on,
 * and until now it was only true over HTTP: `createRole`, `deleteRole` and `setRoleClearance`
 * existed in `api.ts`, against endpoints that exist and are tested, and no screen called any
 * of them.
 */
describe("making a role", () => {
  it("creates it with nothing granted", async () => {
    // The safe end to start from. A form that offered permissions at creation would be a role
    // granted in a single gesture nobody reviews; the matrix below is where it is given
    // anything, one deliberate click at a time.
    // With a role already on screen there is a non-empty permission catalogue to draw from,
    // which is what makes "nothing granted" an assertion rather than a restatement of an
    // empty list.
    roles.mockResolvedValue([role({ permissions: ["query.execute", "roles.manage"] })]);
    createRole.mockResolvedValue(role({ id: "r2", name: "reviewer" }));

    render(<RolePanel token="t" />);
    fireEvent.change(await screen.findByLabelText("New role name"), {
      target: { value: "reviewer" },
    });
    fireEvent.click(screen.getByRole("button", { name: /New role/ }));

    await waitFor(() => expect(createRole).toHaveBeenCalled());
    expect(createRole.mock.calls[0]?.[1]).toMatchObject({
      name: "reviewer",
      permissions: [],
      priority_level: 0,
    });
  });

  it("will not submit an empty name", async () => {
    roles.mockResolvedValue([]);

    render(<RolePanel token="t" />);
    const submit = await screen.findByRole("button", { name: /New role/ });

    expect((submit as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("removing a role", () => {
  it("offers no delete on a system role", async () => {
    roles.mockResolvedValue([role({ id: "sys", name: "admin", is_system: true })]);

    render(<RolePanel token="t" />);
    await screen.findByText("admin");

    expect(screen.queryByRole("button", { name: "Delete admin" })).toBeNull();
  });

  it("shows the server's refusal verbatim", async () => {
    // "this would leave nobody in the tenant holding: roles.manage" tells an administrator
    // exactly what to do first. A sentence of our own would not.
    roles.mockResolvedValue([role()]);
    deleteRole.mockRejectedValue(
      new ApiError(409, "conflict", "this would leave nobody in the tenant holding: roles.manage"),
    );

    render(<RolePanel token="t" />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete editor" }));

    expect(await screen.findByText(/nobody in the tenant holding: roles.manage/)).toBeTruthy();
  });
});

describe("clearance", () => {
  it("can be set on an ordinary role", async () => {
    roles.mockResolvedValue([role()]);
    setRoleClearance.mockResolvedValue(role({ priority_level: 5 }));

    render(<RolePanel token="t" />);
    fireEvent.change(await screen.findByLabelText("Clearance of editor"), {
      target: { value: "5" },
    });

    await waitFor(() => expect(setRoleClearance).toHaveBeenCalledWith("t", "r1", 5));
  });

  it("is locked on a system role", async () => {
    roles.mockResolvedValue([role({ id: "sys", name: "admin", is_system: true })]);

    render(<RolePanel token="t" />);

    expect(
      (await screen.findByLabelText("Clearance of admin")).hasAttribute("disabled"),
    ).toBe(true);
  });
});
