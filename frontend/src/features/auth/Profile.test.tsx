/**
 * The two controls on this screen that end sessions, and the one honest sentence next to them.
 *
 * Both call `onSignedOut`, which is how the shell drops its tokens and shows the login form.
 * A path that forgot to would leave somebody sitting in a workspace whose every request is
 * about to start failing — and, after a password change, with the *old* password's session
 * still on screen, which reads as the change not having worked.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const profile = vi.fn();
const changePassword = vi.fn();
const signOutEverywhere = vi.fn();
const updateProfile = vi.fn();

vi.mock("./api", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("./api");
  return {
    ...actual,
    profile: (...args: unknown[]) => profile(...args),
    changePassword: (...args: unknown[]) => changePassword(...args),
    signOutEverywhere: (...args: unknown[]) => signOutEverywhere(...args),
    updateProfile: (...args: unknown[]) => updateProfile(...args),
  };
});

const { Profile } = await import("./Profile");

beforeEach(() => {
  profile.mockReset();
  changePassword.mockReset();
  signOutEverywhere.mockReset();
  updateProfile.mockReset();
  profile.mockResolvedValue({
    user_id: "u1",
    email: "someone@example.com",
    name: "Someone",
    tenant_id: "t1",
    tenant_name: "Acme",
    roles: ["member"],
    permissions: ["query.execute"],
    labels: ["General"],
    documents_uploaded: 3,
    created_at: "2026-01-01T00:00:00Z",
    is_system_admin: false,
  });
});

const show = async (onSignedOut = vi.fn()) => {
  render(<Profile token="t" onSignedOut={onSignedOut} onProfile={vi.fn()} />);
  await screen.findByText("someone@example.com");
  return onSignedOut;
};

describe("changing the password", () => {
  it("signs the user out, because the change already ended every session", async () => {
    // The endpoint bumps `token_version`, so this session's own tokens stop renewing. Staying
    // on the screen would leave somebody in a workspace that is about to fail every request,
    // which reads as the change not having worked.
    changePassword.mockResolvedValue(undefined);
    const onSignedOut = await show();

    fireEvent.change(screen.getByLabelText(/current password/i), {
      target: { value: "old-one" },
    });
    fireEvent.change(screen.getByLabelText(/new password/i), { target: { value: "a-new-one" } });
    fireEvent.submit(screen.getByLabelText(/current password/i).closest("form")!);

    await waitFor(() => expect(onSignedOut).toHaveBeenCalled());
  });

  it("keeps the user where they are when the current password is wrong", async () => {
    changePassword.mockRejectedValue(new Error("that is not your current password"));
    const onSignedOut = await show();

    fireEvent.change(screen.getByLabelText(/current password/i), { target: { value: "wrong" } });
    fireEvent.change(screen.getByLabelText(/new password/i), { target: { value: "a-new-one" } });
    fireEvent.submit(screen.getByLabelText(/current password/i).closest("form")!);

    await waitFor(() => expect(changePassword).toHaveBeenCalled());
    // Signing them out on a refusal would be the worst possible reading of a failed attempt.
    expect(onSignedOut).not.toHaveBeenCalled();
  });
});

describe("signing out everywhere", () => {
  it("ends this session too once the server has answered", async () => {
    signOutEverywhere.mockResolvedValue(undefined);
    const onSignedOut = await show();

    fireEvent.click(screen.getByRole("button", { name: /sign out everywhere/i }));

    await waitFor(() => expect(onSignedOut).toHaveBeenCalled());
  });

  it("does not sign the user out when the request failed", async () => {
    // Dropping the tokens on a failed revocation would leave every *other* session alive
    // while removing the one place the person could try again from.
    signOutEverywhere.mockRejectedValue(new Error("network"));
    const onSignedOut = await show();

    fireEvent.click(screen.getByRole("button", { name: /sign out everywhere/i }));

    await waitFor(() => expect(signOutEverywhere).toHaveBeenCalled());
    expect(onSignedOut).not.toHaveBeenCalled();
    expect(await screen.findByRole("alert")).toBeTruthy();
  });

  it("promises only what stateless tokens can deliver", async () => {
    // Access tokens are stateless by design, so this ends the ability to *renew* a session
    // rather than killing one mid-flight. A security control is the worst place to overstate,
    // and the wording is the control's honesty rather than decoration.
    await show();

    expect(screen.getByText(/once their access token expires/i)).toBeTruthy();
  });
});
