/**
 * The one screen where a click can end a customer.
 *
 * Most of these assertions are about what the screen *refuses* to do. The server enforces
 * every rule independently — suspended-before-purge, name-must-match — so nothing here is
 * the guarantee. What it is, is the difference between an operator understanding what they
 * are about to do and finding out afterwards.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { System } from "./System";

const ACTIVE = {
  id: "t1",
  name: "Acme SA",
  status: "active" as const,
  created_at: "2026-01-15T10:00:00Z",
  status_changed_at: null,
  users: 12,
  documents: 340,
  storage_bytes: 2_400_000_000,
};

const SUSPENDED = { ...ACTIVE, id: "t2", name: "Beta Ltd", status: "suspended" as const, users: 3 };

const organisations = vi.fn();
const suspendOrganisation = vi.fn();
const activateOrganisation = vi.fn();
const purgeOrganisation = vi.fn();
const provisionOrganisation = vi.fn();

vi.mock("./api", () => ({
  organisations: (...a: unknown[]) => organisations(...a),
  suspendOrganisation: (...a: unknown[]) => suspendOrganisation(...a),
  activateOrganisation: (...a: unknown[]) => activateOrganisation(...a),
  purgeOrganisation: (...a: unknown[]) => purgeOrganisation(...a),
  provisionOrganisation: (...a: unknown[]) => provisionOrganisation(...a),
}));

beforeEach(() => {
  vi.clearAllMocks();
  organisations.mockResolvedValue([ACTIVE, SUSPENDED]);
  suspendOrganisation.mockResolvedValue({ ...ACTIVE, status: "suspended" });
  activateOrganisation.mockResolvedValue({ ...SUSPENDED, status: "active" });
  purgeOrganisation.mockResolvedValue({ ...SUSPENDED, status: "purging" });
});

describe("the list", () => {
  it("shows each organisation with what is behind it", async () => {
    render(<System token="t" />);

    expect(await screen.findByText("Acme SA")).toBeTruthy();
    expect(screen.getByText(/12 users · 340 documents · 2\.4 GB/)).toBeTruthy();
  });

  it("shows the state of each one", async () => {
    render(<System token="t" />);

    expect(await screen.findByText("active")).toBeTruthy();
    expect(screen.getByText("suspended")).toBeTruthy();
  });
});

describe("suspending", () => {
  it("is offered on an active organisation", async () => {
    render(<System token="t" />);

    fireEvent.click(await screen.findByRole("button", { name: "Suspend" }));

    await waitFor(() => expect(suspendOrganisation).toHaveBeenCalledWith("t", "t1"));
  });

  it("re-reads the list instead of trusting its own copy", async () => {
    // The server owns the lifecycle. A screen that decides for itself whether a customer
    // is switched off can be wrong about it.
    render(<System token="t" />);

    fireEvent.click(await screen.findByRole("button", { name: "Suspend" }));

    await waitFor(() => expect(organisations).toHaveBeenCalledTimes(2));
  });

  it("reports a refusal rather than showing the change as made", async () => {
    suspendOrganisation.mockRejectedValue(new Error("an organisation that is purging cannot"));
    render(<System token="t" />);

    fireEvent.click(await screen.findByRole("button", { name: "Suspend" }));

    expect(await screen.findByRole("alert")).toBeTruthy();
  });
});

describe("purging", () => {
  it("is not offered on an active organisation", async () => {
    // The reversible act has to happen first, and be seen to work. The server refuses too.
    organisations.mockResolvedValue([ACTIVE]);
    render(<System token="t" />);
    await screen.findByText("Acme SA");

    expect(screen.queryByRole("button", { name: "Purge" })).toBeNull();
  });

  it("is offered once suspended", async () => {
    render(<System token="t" />);

    expect(await screen.findByRole("button", { name: "Purge" })).toBeTruthy();
  });

  it("says what is about to be destroyed", async () => {
    render(<System token="t" />);
    fireEvent.click(await screen.findByRole("button", { name: "Purge" }));

    expect(screen.getByText(/3 users, 340 documents/)).toBeTruthy();
    expect(screen.getByText(/cannot be undone/i)).toBeTruthy();
  });

  it("will not proceed until the name is typed exactly", async () => {
    render(<System token="t" />);
    fireEvent.click(await screen.findByRole("button", { name: "Purge" }));

    const confirm = screen.getByRole("button", { name: "Purge permanently" });
    expect((confirm as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText(/Type/), { target: { value: "beta ltd" } });
    expect((confirm as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText(/Type/), { target: { value: "Beta Ltd" } });
    expect((confirm as HTMLButtonElement).disabled).toBe(false);
  });

  it("sends the typed name so the server can check it too", async () => {
    render(<System token="t" />);
    fireEvent.click(await screen.findByRole("button", { name: "Purge" }));
    fireEvent.change(screen.getByLabelText(/Type/), { target: { value: "Beta Ltd" } });
    fireEvent.click(screen.getByRole("button", { name: "Purge permanently" }));

    await waitFor(() => expect(purgeOrganisation).toHaveBeenCalledWith("t", "t2", "Beta Ltd"));
  });

  it("can be backed out of", async () => {
    render(<System token="t" />);
    fireEvent.click(await screen.findByRole("button", { name: "Purge" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(purgeOrganisation).not.toHaveBeenCalled();
  });
});

describe("provisioning", () => {
  it("shows the generated password once, with the warning", async () => {
    provisionOrganisation.mockResolvedValue({
      organisation: ACTIVE,
      admin_email: "admin@acme.test",
      password: "s3cret-once",
    });
    render(<System token="t" />);
    await screen.findByText("Acme SA");

    fireEvent.change(screen.getByLabelText("Organisation name"), { target: { value: "Acme SA" } });
    fireEvent.change(screen.getByLabelText("First administrator's email"), {
      target: { value: "admin@acme.test" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByText("s3cret-once")).toBeTruthy();
    expect(screen.getByText(/cannot be shown again/i)).toBeTruthy();
  });

  it("will not submit an incomplete form", async () => {
    render(<System token="t" />);
    await screen.findByText("Acme SA");

    expect((screen.getByRole("button", { name: "Create" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });
});
