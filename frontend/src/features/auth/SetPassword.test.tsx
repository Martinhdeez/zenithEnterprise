/** Invitation validation must be announced in the chosen language before a link is spent. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { setLanguage } from "@/shared/i18n/useT";

const describeCredential = vi.fn();
const redeemCredential = vi.fn();

vi.mock("./api", () => ({
  describeCredential: (...args: unknown[]) => describeCredential(...args),
  redeemCredential: (...args: unknown[]) => redeemCredential(...args),
}));

const { SetPassword } = await import("./SetPassword");

beforeEach(() => {
  setLanguage("es");
  describeCredential.mockReset();
  redeemCredential.mockReset();
  describeCredential.mockResolvedValue({ email: "invited@example.com", purpose: "invitation" });
});

afterEach(() => {
  cleanup();
  setLanguage("en");
});

async function invitation() {
  render(<SetPassword token="disposable-test-link" />);
  await screen.findByRole("button", { name: "Establecer contraseña" });
}

describe("Spanish invitation accessibility", () => {
  it("announces a short-password error without spending the credential", async () => {
    await invitation();
    fireEvent.change(screen.getByLabelText("Contraseña nueva"), { target: { value: "short" } });
    fireEvent.change(screen.getByLabelText("Repite la contraseña"), { target: { value: "short" } });
    fireEvent.click(screen.getByRole("button", { name: "Establecer contraseña" }));

    expect((await screen.findByRole("alert")).textContent).toBe("Usa al menos 12 caracteres.");
    expect(redeemCredential).not.toHaveBeenCalled();
    expect(screen.getByText("Al menos 12 caracteres. Nadie más la ve, ni siquiera el administrador que te invitó.")).toBeTruthy();
  });

  it("announces mismatching passwords without spending the credential", async () => {
    await invitation();
    fireEvent.change(screen.getByLabelText("Contraseña nueva"), { target: { value: "a-long-password" } });
    fireEvent.change(screen.getByLabelText("Repite la contraseña"), { target: { value: "a-different-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Establecer contraseña" }));

    expect((await screen.findByRole("alert")).textContent).toBe("Las dos contraseñas no coinciden.");
    expect(redeemCredential).not.toHaveBeenCalled();
  });

  it("explains an unusable link in Spanish", async () => {
    describeCredential.mockRejectedValue(new Error("unissued"));
    render(<SetPassword token="unissued-test-link" />);

    await screen.findByRole("heading", { name: "Este enlace ya no funciona" });
    expect(screen.getByText("Los enlaces sólo se pueden usar una vez y caducan. Pide a tu administrador que envíe uno nuevo.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Establecer contraseña" })).toBeNull();
  });

  it("separates the account email from the translated introductory text", async () => {
    await invitation();
    expect(screen.getByText("invited@example.com").parentElement?.textContent).toBe(
      "Estableciendo la contraseña de invited@example.com.",
    );
  });
});
