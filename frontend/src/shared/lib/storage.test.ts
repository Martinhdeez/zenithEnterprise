/**
 * A browser that refuses site data must cost a preference, never the product.
 *
 * Three unguarded accesses were found together, and the two in `App.tsx` were fatal: an
 * unguarded read in the render path meant Safari's private browsing — or any enterprise
 * policy blocking site data — did not degrade the application, it stopped it from rendering.
 *
 * Each test here reproduces one of the ways a browser actually refuses: the property throwing
 * on access, the method throwing on call, and the object simply not being there.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { forget, read, write } from "./storage";

afterEach(() => {
  vi.unstubAllGlobals();
});

/** A store whose every method throws, which is how a full quota presents. */
const refusing = {
  getItem: () => {
    throw new DOMException("QuotaExceededError");
  },
  setItem: () => {
    throw new DOMException("QuotaExceededError");
  },
  removeItem: () => {
    throw new DOMException("QuotaExceededError");
  },
};

describe("when storage works", () => {
  it("round-trips a value", () => {
    write("session", "zenith.probe", "kept");

    expect(read("session", "zenith.probe")).toBe("kept");
  });

  it("forgets one", () => {
    write("session", "zenith.probe", "kept");

    forget("session", "zenith.probe");

    expect(read("session", "zenith.probe")).toBeNull();
  });

  it("reports a key that was never set as absent rather than undefined", () => {
    // The callers compare against a string; `undefined` would make `=== "true"` behave the
    // same way by luck and a `??` fallback behave differently.
    expect(read("local", "zenith.never-written")).toBeNull();
  });
});

describe("when the browser refuses", () => {
  it("reads as absent when the method throws", () => {
    vi.stubGlobal("localStorage", refusing);

    expect(read("local", "zenith.probe")).toBeNull();
  });

  it("writes without throwing when the quota is full", () => {
    vi.stubGlobal("localStorage", refusing);

    expect(() => write("local", "zenith.probe", "value")).not.toThrow();
  });

  it("forgets without throwing", () => {
    // The one `Search` left bare: clearing the recent-search list threw out of a click
    // handler on a browser blocking storage.
    vi.stubGlobal("localStorage", refusing);

    expect(() => forget("local", "zenith.probe")).not.toThrow();
  });

  it("survives the store being absent entirely", () => {
    vi.stubGlobal("localStorage", undefined);

    expect(read("local", "zenith.probe")).toBeNull();
    expect(() => write("local", "zenith.probe", "value")).not.toThrow();
    expect(() => forget("local", "zenith.probe")).not.toThrow();
  });

  it("survives the property itself throwing on access", () => {
    // Not a hypothetical: some hardened browser configurations make the *getter* throw, so
    // reading `localStorage` at all is the failure and no method is ever reached.
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() {
        throw new DOMException("SecurityError");
      },
    });

    expect(read("local", "zenith.probe")).toBeNull();
    expect(() => write("local", "zenith.probe", "value")).not.toThrow();
  });
});
