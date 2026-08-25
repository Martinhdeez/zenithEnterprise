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

const roles = vi.fn();
const setRolePermissions = vi.fn();

vi.mock("../api", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("../api");
  return {
    ...actual,
    roles: (...args: unknown[]) => roles(...args),
    setRolePermissions: (...args: unknown[]) => setRolePermissions(...args),
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
