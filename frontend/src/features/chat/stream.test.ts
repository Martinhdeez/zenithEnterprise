/**
 * Parsing SSE from a `fetch` body.
 *
 * The hard case is not the happy path — it is that frames arrive split at arbitrary byte
 * boundaries. A parser that assumes one chunk contains one whole frame works against a fast
 * local server and fails against the one behind a customer's proxy.
 */

import { describe, expect, it, vi } from "vitest";

import { streamQuery, type QueryResult } from "./stream";

function bodyOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

function respondWith(chunks: string[]): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(bodyOf(chunks), { status: 200 })),
  );
}

const RESULT: QueryResult = {
  query_id: "q1",
  answer: "Within 72 hours [1].",
  citations: [],
  abstained: false,
  consulted: [],
  model: "llama3.1:8b",
  degraded: false,
  reason: null,
  took_retrieval_ms: 1200,
  took_generation_ms: 9000,
};

async function collect(chunks: string[]) {
  respondWith(chunks);
  const tokens: string[] = [];
  const results: QueryResult[] = [];
  const errors: string[] = [];
  await streamQuery("q", "token", {
    onToken: (text) => tokens.push(text),
    onResult: (result) => results.push(result),
    onError: (message) => errors.push(message),
  });
  return { tokens, results, errors };
}

describe("the SSE reader", () => {
  it("reads whole frames", async () => {
    const { tokens, results } = await collect([
      "event: token\ndata: Within 72\n\n",
      "event: token\ndata:  hours.\n\n",
      `event: result\ndata: ${JSON.stringify(RESULT)}\n\n`,
    ]);

    expect(tokens).toEqual(["Within 72", " hours."]);
    expect(results).toHaveLength(1);
    expect(results[0]?.answer).toBe("Within 72 hours [1].");
  });

  it("reassembles a frame split across chunks", async () => {
    // The case that decides the implementation. A network delivers bytes, not frames.
    const { tokens, results } = await collect([
      "event: tok",
      "en\ndata: Within",
      " 72 hours.\n",
      "\nevent: result\ndata: ",
      `${JSON.stringify(RESULT)}\n\n`,
    ]);

    expect(tokens).toEqual(["Within 72 hours."]);
    expect(results).toHaveLength(1);
  });

  it("preserves a token that is only a space", async () => {
    // Trimming the data value would glue two words together in the middle of an answer.
    // Exactly one leading space is stripped, because that is the SSE field separator.
    const { tokens } = await collect(["event: token\ndata:  \n\n"]);

    expect(tokens).toEqual([" "]);
  });

  it("ignores an event type it does not recognise", async () => {
    // The endpoint documents that a client may ignore unknown events, which is what lets a
    // fourth event type be added without breaking every deployed client.
    const { tokens, results, errors } = await collect([
      "event: heartbeat\ndata: ping\n\n",
      "event: token\ndata: text\n\n",
      `event: result\ndata: ${JSON.stringify(RESULT)}\n\n`,
    ]);

    expect(tokens).toEqual(["text"]);
    expect(results).toHaveLength(1);
    expect(errors).toEqual([]);
  });

  it("reports a rejected request through onError rather than throwing", async () => {
    // A 503 here means no model is configured. The chat component has to render that as a
    // message, not as an unhandled rejection.
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({ code: "generation_unavailable", message: "No model configured." }),
            { status: 503 },
          ),
      ),
    );
    const errors: string[] = [];

    await streamQuery("q", "token", {
      onToken: () => undefined,
      onResult: () => undefined,
      onError: (message) => errors.push(message),
    });

    expect(errors).toEqual(["No model configured."]);
  });
});
