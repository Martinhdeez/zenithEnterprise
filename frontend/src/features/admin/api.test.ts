/**
 * The one call on this screen where sending a field and omitting it mean different things.
 */

import { describe, expect, it, vi } from "vitest";

import { saveLlmConfig } from "./api";

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
