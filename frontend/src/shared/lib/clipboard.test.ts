/**
 * The rule this file exists for: on a plain-HTTP installation there is no
 * `navigator.clipboard` at all, and that is the normal deployment for this product rather
 * than an edge case.
 *
 * Both failure directions are asserted, because both were live in the tree before this
 * helper: one call site used `?.` and copied nothing in silence, another used no guard and
 * threw. A caller that cannot tell success from silence is the thing being prevented.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { copy } from "./clipboard";

const clipboard = (value: unknown) =>
  Object.defineProperty(navigator, "clipboard", { value, configurable: true });

afterEach(() => {
  vi.restoreAllMocks();
});

describe("copying", () => {
  it("uses the clipboard API when the context is secure", async () => {
    const writeText = vi.fn(async () => {});
    clipboard({ writeText });

    expect(await copy("plazo máximo")).toBe(true);
    expect(writeText).toHaveBeenCalledWith("plazo máximo");
  });

  it("falls back when the API is absent, which is what plain HTTP looks like", async () => {
    clipboard(undefined);
    const exec = vi.fn(() => true);
    document.execCommand = exec as never;

    expect(await copy("cuarenta horas")).toBe(true);
    expect(exec).toHaveBeenCalledWith("copy");
  });

  it("falls back when the API is present and refuses", async () => {
    // A context that reports the API and then denies permission. Giving up here would be
    // the silent failure; the older path frequently still works.
    clipboard({ writeText: vi.fn(async () => Promise.reject(new Error("denied"))) });
    document.execCommand = vi.fn(() => true) as never;

    expect(await copy("texto")).toBe(true);
  });

  it("reports failure rather than claiming success", async () => {
    clipboard(undefined);
    document.execCommand = vi.fn(() => false) as never;

    // The caller shows "Copied" on `true`. Returning `true` here would put the word on
    // screen over an empty clipboard, which is worse than no button.
    expect(await copy("texto")).toBe(false);
  });
});
