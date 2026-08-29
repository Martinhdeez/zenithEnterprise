/**
 * The stale-tab case, which is what happens every time this product is updated in place
 * while somebody has it open.
 *
 * Vite names split chunks by content hash and a new build does not keep the old names, so a
 * tab running the previous bundle asks for a file that is gone the first time it needs the
 * viewer. What the reader sees is everything working except the one code-split feature.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import { chunkLoaded, lazyChunk } from "./lazyChunk";

const reload = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, reload },
  });
});

describe("loading a chunk", () => {
  it("returns the module when the chunk is there", async () => {
    const module = { default: "viewer" };

    await expect(lazyChunk(async () => module)()).resolves.toBe(module);
    expect(reload).not.toHaveBeenCalled();
  });

  it("reloads when the chunk is gone, rather than throwing at the reader", async () => {
    // The application has been replaced underneath this tab. Fetching the replacement is
    // what the reader wants; an error naming a missing `.js` file is not something they
    // can act on.
    const load = lazyChunk(async () => {
      throw new TypeError("Failed to fetch dynamically imported module");
    });

    void load();
    await vi.waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
  });

  it("never settles once it has decided to reload", async () => {
    // Resolving with anything would render a component for the frame before the page goes
    // away, and the obvious wrong choice — resolving with `undefined` — renders nothing at
    // all, which looks exactly like the bug being fixed.
    const settled = vi.fn();
    const load = lazyChunk(async () => {
      throw new Error("gone");
    });

    void load().then(settled, settled);
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(settled).not.toHaveBeenCalled();
  });
});

describe("when reloading did not fix it", () => {
  it("lets the error through instead of reloading again", async () => {
    // The chunk is genuinely unreachable — the server is down, or the network is. Reloading
    // a second time would be a loop the reader cannot escape, and this is the assertion
    // that stops it being written.
    const load = lazyChunk(async () => {
      throw new Error("still gone");
    });
    void load();
    await vi.waitFor(() => expect(reload).toHaveBeenCalledTimes(1));

    await expect(load()).rejects.toThrow("still gone");
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("allows a fresh reload once a chunk has loaded successfully", async () => {
    // Otherwise one recovered failure would disarm the recovery for the rest of the
    // session, and the second deployment of the day would be an error again.
    sessionStorage.setItem("zenith.chunk-reload", "1");

    chunkLoaded();
    const load = lazyChunk(async () => {
      throw new Error("gone");
    });
    void load();

    await vi.waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
  });
});
