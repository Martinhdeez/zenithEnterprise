/**
 * The link is returned once and cannot be recovered.
 *
 * That single fact shapes the whole panel: it *replaces* the form rather than sitting beside
 * it, and it shows the link in full. An administrator who closes it without copying has to
 * invite again, and that is only cheap if nothing else on screen is competing for their
 * attention at the moment it appears.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const inviteUser = vi.fn();
const roles = vi.fn();

vi.mock("../api", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("../api");
  return {
    ...actual,
    inviteUser: (...args: unknown[]) => inviteUser(...args),
    roles: (...args: unknown[]) => roles(...args),
  };
});

const { InvitePanel } = await import("./InvitePanel");

const invite = async (email: string) => {
  render(<InvitePanel token="t" />);
  const field = await screen.findByLabelText("Email");
  fireEvent.change(field, { target: { value: email } });
  fireEvent.submit(field.closest("form")!);
};

beforeEach(() => {
  inviteUser.mockReset();
  roles.mockReset();
  roles.mockResolvedValue([]);
  inviteUser.mockResolvedValue({
    email: "new@example.com",
    path: "/set-password/abc123",
    expires_at: "2026-09-01T10:00:00Z",
  });
});

describe("once the invitation is issued", () => {
  it("replaces the form rather than showing both", async () => {
    await invite("new@example.com");

    expect(await screen.findByText(/Send this link to new@example.com/)).toBeTruthy();
    expect(screen.queryByLabelText("Email")).toBeNull();
  });

  it("shows the link whole, because the point of it is to be copied", async () => {
    // A link with an ellipsis in the middle cannot be copied out of the page by hand, and
    // the clipboard button is not always available — an insecure origin has no
    // `navigator.clipboard` at all.
    await invite("new@example.com");

    const shown = await screen.findByText(/set-password\/abc123/);
    expect(shown.textContent).toContain("/set-password/abc123");
  });

  it("can be dismissed back to the form for the next colleague", async () => {
    await invite("new@example.com");
    fireEvent.click(await screen.findByRole("button", { name: /Done/i }));

    expect(await screen.findByLabelText("Email")).toBeTruthy();
  });
});

describe("when the server refuses", () => {
  it("keeps the form and does not pretend an invitation exists", async () => {
    // The failure that matters: an address already registered elsewhere. Showing a link
    // panel with nothing in it would be worse than the error.
    inviteUser.mockRejectedValue(new Error("that address is already registered"));

    await invite("taken@example.com");

    await waitFor(() => expect(inviteUser).toHaveBeenCalled());
    expect(screen.queryByText(/Send this link/)).toBeNull();
    expect(screen.getByLabelText("Email")).toBeTruthy();
  });
});
