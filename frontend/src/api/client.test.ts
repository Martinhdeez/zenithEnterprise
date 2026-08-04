/**
 * The parts of the client that are contracts rather than plumbing.
 */

import { describe, expect, it, vi } from "vitest";

import { IN_FLIGHT, saveLlmConfig } from "./client";

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

describe("saving the LLM configuration", () => {
  it("omits the key entirely when none was entered", async () => {
    // The server reads omission as "keep the stored key" and "" as "clear it". Sending an
    // empty string for an untouched field would wipe a credential the administrator cannot
    // read, on a save that only changed the model name.
    // Typed as the real signature so the call arguments are readable: `vi.fn` with no
    // annotation infers an empty tuple and indexing it is a type error rather than a
    // test failure.
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Response("{}", { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await saveLlmConfig("token", { endpoint_url: "http://x/v1", model_name: "m" });

    const body: Record<string, unknown> = JSON.parse(
      String(fetchMock.mock.calls[0]?.[1]?.body),
    );
    expect("api_key" in body).toBe(false);
  });

  it("sends the key when one was entered", async () => {
    // Typed as the real signature so the call arguments are readable: `vi.fn` with no
    // annotation infers an empty tuple and indexing it is a type error rather than a
    // test failure.
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Response("{}", { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await saveLlmConfig("token", {
      endpoint_url: "http://x/v1",
      model_name: "m",
      api_key: "sk-new",
    });

    const body: Record<string, unknown> = JSON.parse(
      String(fetchMock.mock.calls[0]?.[1]?.body),
    );
    expect(body.api_key).toBe("sk-new");
  });
});
