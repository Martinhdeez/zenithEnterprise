/**
 * The parts of the HTTP layer that are contracts rather than plumbing.
 */

import { describe, expect, it, vi } from "vitest";

import { request } from "./http";
import { IN_FLIGHT } from "./tenant";

describe("document statuses", () => {
  it("mirrors the statuses the schema defines", () => {
    // A real duplication of the backend's DOCUMENT_STATUSES, because the client cannot
    // import Python. `documents` is keyed by whatever the server sends, so a typo here is
    // not a type error — it is a counter that reads zero forever, which is exactly what
    // the first version of the status badge did with an invented `processing`.
    expect([...IN_FLIGHT]).toEqual(["pending", "parsing", "chunking", "embedding"]);
    expect(IN_FLIGHT).not.toContain("processing");
    expect(IN_FLIGHT).not.toContain("ready");
    expect(IN_FLIGHT).not.toContain("failed");
  });
});

describe("error handling", () => {
  it("prefers RFC 7807 detail and falls back to the legacy message", async () => {
    // Both are read because the server sends both: `detail` is the standard member and
    // `message` is retained for clients written against the original shape. Preferring
    // detail with message as fallback means this client works against either.
    const { ApiError } = await import("./http");
    const { tenantStatus } = await import("./tenant");

    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              type: "https://zenith.enterprise/problems/permission_denied",
              title: "Permission denied",
              status: 403,
              detail: "you do not hold label(s): finance",
              code: "permission_denied",
              message: "you do not hold label(s): finance",
            }),
            { status: 403 },
          ),
      ),
    );

    await expect(tenantStatus("token")).rejects.toMatchObject({
      code: "permission_denied",
      message: "you do not hold label(s): finance",
    });
    await expect(tenantStatus("token")).rejects.toBeInstanceOf(ApiError);
  });

  it("survives a server that sends no body at all", async () => {
    // A proxy returning a bare 502 is not a hypothetical on an on-premise install, and an
    // unparseable body must not become an unhandled rejection in a UI.
    const { tenantStatus } = await import("./tenant");
    vi.stubGlobal("fetch", vi.fn(async () => new Response("not json", { status: 502 })));

    await expect(tenantStatus("token")).rejects.toMatchObject({ code: "unknown" });
  });
});

describe("what a failure says", () => {
  it("shows the sentence this API's own errors carry", async () => {
    globalThis.fetch = vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ code: "conflict", detail: "Suspend it first" }), {
          status: 409,
        }),
      ),
    ) as unknown as typeof fetch;

    await expect(request("/x", "t")).rejects.toThrow("Suspend it first");
  });

  it("turns FastAPI's validation array into readable text", async () => {
    // A 422 answers with `[{loc, msg, type}]`, not a string. Rendered straight it put the
    // literal `[object Object]` on screen where the reason should be — on every form in
    // the product, not just the one that happened to find it.
    globalThis.fetch = vi.fn(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            detail: [
              { loc: ["body", "admin_email"], msg: "value is not a valid email address" },
              { loc: ["body", "name"], msg: "String should have at least 1 character" },
            ],
          }),
          { status: 422 },
        ),
      ),
    ) as unknown as typeof fetch;

    // Both, joined: a form can fail two fields at once, and hearing about one means
    // submitting again to discover the other.
    await expect(request("/x", "t")).rejects.toThrow(
      "value is not a valid email address. String should have at least 1 character",
    );
  });

  it("falls back to a sentence when the shape is unrecognisable", async () => {
    globalThis.fetch = vi.fn(() =>
      Promise.resolve(new Response(JSON.stringify({ detail: { odd: true } }), { status: 500 })),
    ) as unknown as typeof fetch;

    await expect(request("/x", "t")).rejects.toThrow("The request failed.");
  });
});
