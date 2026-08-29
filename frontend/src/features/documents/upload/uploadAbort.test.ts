/**
 * Stopping an upload that is already in flight.
 *
 * The `abort` listener has been in `uploadWithProgress` since it was written, rejecting with
 * `ApiError(0, "aborted")` — and nothing could ever reach it, because there was no way to
 * pass a signal in. It was dead code that looked like a feature. These tests exist so it
 * cannot quietly become dead again.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { uploadDocument } from "../api";
import { ApiError } from "@/shared/api/http";

/** Just enough `XMLHttpRequest` to drive the two paths that matter. */
class FakeXhr {
  static last: FakeXhr | null = null;

  aborted = false;
  sent = false;
  status = 200;
  responseText = "{}";
  upload = { addEventListener: vi.fn() };
  private listeners = new Map<string, (() => void)[]>();

  constructor() {
    FakeXhr.last = this;
  }

  open = vi.fn();
  setRequestHeader = vi.fn();

  addEventListener(event: string, handler: () => void) {
    this.listeners.set(event, [...(this.listeners.get(event) ?? []), handler]);
  }

  removeEventListener(event: string, handler: () => void) {
    this.listeners.set(event, (this.listeners.get(event) ?? []).filter((it) => it !== handler));
  }

  send() {
    this.sent = true;
  }

  abort() {
    this.aborted = true;
    this.fire("abort");
    this.fire("loadend");
  }

  fire(event: string) {
    for (const handler of this.listeners.get(event) ?? []) handler();
  }

  /** Whether anything is still listening for the controller's abort. */
  listenerCount(event: string) {
    return (this.listeners.get(event) ?? []).length;
  }
}

const install = () => {
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
};

const upload = (signal?: AbortSignal) =>
  uploadDocument(new File(["x"], "a.pdf"), "token", [], { onProgress: () => {}, signal });

afterEach(() => {
  vi.unstubAllGlobals();
  FakeXhr.last = null;
});

describe("cancelling an upload in flight", () => {
  it("aborts the request when the signal fires", async () => {
    install();
    const controller = new AbortController();
    const pending = upload(controller.signal);

    controller.abort();

    await expect(pending).rejects.toThrow(ApiError);
    expect(FakeXhr.last?.aborted).toBe(true);
  });

  it("reports the cancellation with a code the caller can switch on", async () => {
    // `Upload.tsx` tells a cancellation from a failure by this code, and paints the row
    // grey instead of red because of it.
    install();
    const controller = new AbortController();
    const pending = upload(controller.signal);

    controller.abort();

    await expect(pending).rejects.toMatchObject({ code: "aborted" });
  });

  it("never sends the bytes when the signal is already aborted", async () => {
    // The queued-file case. Uploading a whole document and aborting a moment later would
    // spend the bandwidth this button exists to save.
    install();
    const controller = new AbortController();
    controller.abort();

    await expect(upload(controller.signal)).rejects.toMatchObject({ code: "aborted" });
    expect(FakeXhr.last).toBeNull();
  });

  it("stops listening to the controller once the request settles", async () => {
    // A controller outlives the upload it belongs to — the map in `Upload.tsx` holds one
    // per row for as long as the batch is on screen. A listener still holding the XHR
    // would keep its `FormData`, and the file's bytes with it, reachable.
    install();
    const controller = new AbortController();
    const pending = upload(controller.signal);

    FakeXhr.last?.fire("load");
    FakeXhr.last?.fire("loadend");
    await pending;

    expect(FakeXhr.last?.listenerCount("abort")).toBe(1); // the XHR's own, not the signal's
    controller.abort();
    expect(FakeXhr.last?.aborted).toBe(false);
  });

  it("leaves an ordinary upload alone when no signal is given", async () => {
    install();
    const pending = upload();

    FakeXhr.last?.fire("load");

    await expect(pending).resolves.toBeDefined();
    expect(FakeXhr.last?.aborted).toBe(false);
  });
});
