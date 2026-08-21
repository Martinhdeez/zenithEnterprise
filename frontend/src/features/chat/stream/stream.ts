/**
 * Reading `POST /query/stream`, and honouring the contract it states.
 *
 * `EventSource` cannot be used here and that is not a stylistic choice: the browser API is
 * GET-only and sends no headers, while this endpoint is a POST carrying a bearer token. So
 * the body is read with `fetch` + `ReadableStream` and SSE frames are parsed here — about
 * thirty lines, and the same decision the backend made rather than taking a dependency to
 * split on a colon.
 *
 * The rule this file exists to keep is written into the endpoint's own OpenAPI description:
 *
 *   A client must not present a streamed answer as final until the `result` event arrives,
 *   and must replace what it displayed if `abstained` is true.
 *
 * The 0% fabrication gate survives streaming only because the client honours that. The
 * backend strips invented citation markers in flight, but whether an answer cites anything
 * valid *at all* is knowable only at the end — so a client that leaves streamed prose on
 * screen under an abstention notice has shipped exactly the fabrication two milestones went
 * into preventing.
 */

export interface Citation {
  marker: number;
  chunk_id: string;
  document_id: string;
  filename: string;
  page_num: number;
  text: string;
  bboxes: Array<Record<string, number>>;
}

export interface Consulted {
  document_id: string;
  filename: string;
  page_num: number;
}

export interface QueryResult {
  query_id: string;
  answer: string;
  citations: Citation[];
  abstained: boolean;
  consulted: Consulted[];
  model: string;
  degraded: boolean;
  reason: string | null;
  took_retrieval_ms: number;
  took_generation_ms: number;
}

export interface StreamHandlers {
  /** A piece of validated answer text. Never contains an invented citation marker. */
  onToken: (text: string) => void;
  /**
   * The authoritative verdict. The caller MUST render `result.answer` rather than the
   * accumulated tokens — see the module docstring.
   */
  onResult: (result: QueryResult) => void;
  onError: (message: string) => void;
}

/** Frames are separated by a blank line; `event:` and `data:` are the only fields used. */
function parseFrame(frame: string): { event: string; data: string } | null {
  let event = "message";
  const data: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    // Not `.trim()` on the value: a token may legitimately be a single space, and trimming
    // it would silently glue two words together in the middle of an answer.
    else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
  }
  if (data.length === 0) return null;
  return { event, data: data.join("\n") };
}

/** One exchange already on screen, sent so the next message can refer to it. */
export interface Turn {
  question: string;
  answer: string;
}

export async function streamQuery(
  question: string,
  token: string,
  handlers: StreamHandlers,
  options: {
    labels?: string[];
    signal?: AbortSignal;
    history?: Turn[];
    /** Document ids from the composer's `@` mentions. The answer is then grounded in
        these documents and nothing else — enforced in the retrieval SQL, not here. */
    documents?: string[];
  } = {},
): Promise<void> {
  // A relative path deliberately. The client is served next to the API inside the
  // customer's network, and a baked-in host is a value that is wrong on every installation
  // except the one it was built for.
  const response = await fetch("/query/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    // `history` is this thread, not the stored query log: the server bounds it again
    // before it reaches the model, so sending more here cannot enlarge the prompt.
    body: JSON.stringify({
      question,
      labels: options.labels ?? null,
      history: options.history ?? [],
      // `null` rather than `[]` when nothing is mentioned: an empty array and "no scope"
      // are the same thing to this API, but sending the field only when it means something
      // keeps an unscoped request byte-identical to what it was before mentions existed.
      documents: options.documents?.length ? options.documents : null,
    }),
    signal: options.signal,
  });

  if (!response.ok || !response.body) {
    // The backend never forwards a provider's error body, so whatever arrives here is
    // already safe to show — but it is still an internal message, so only the shape the
    // API documents is read.
    // RFC 7807: `detail` describes this occurrence. `message` is the retained legacy
    // member, read as a fallback so this client also works against an older server.
    const problem = await response.json().catch(() => ({}));
    handlers.onError(problem.detail ?? problem.message ?? "The request failed.");
    return;
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += value;
    // `sse-starlette` (the server's SSE library) terminates lines with `\r\n`, not bare
    // `\n` — both are valid per the SSE spec, but every boundary and field check below
    // was written assuming the latter. Without this, `buffer.indexOf("\n\n")` never
    // matches inside a `\r\n\r\n` blank line, no frame is ever considered complete, and
    // the whole answer sits in `buffer` until the connection closes and this loop exits
    // having called neither `onToken` nor `onResult` — a client that looks like it's
    // still waiting for an answer the server already finished sending. Safe to run every
    // iteration on the full accumulated buffer: a `\r\n` split across two reads leaves a
    // lone trailing `\r` that this same replace resolves as soon as the matching `\n`
    // arrives in the next chunk.
    buffer = buffer.replace(/\r\n/g, "\n");

    // Frames arrive split across chunks in arbitrary places, so the buffer is only ever
    // consumed up to the last complete frame boundary.
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const frame = parseFrame(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
      if (!frame) continue;

      if (frame.event === "token") {
        handlers.onToken(frame.data);
      } else if (frame.event === "result") {
        handlers.onResult(JSON.parse(frame.data) as QueryResult);
      } else if (frame.event === "error") {
        handlers.onError(frame.data);
      }
      // Any other event is ignored on purpose: the endpoint documents that a client may
      // ignore what it does not recognise, which is what lets a fourth event be added
      // without breaking every deployed client.
    }
  }
}
