# F12 — The web client, designed around numbers rather than hopes

F11 produced the first latency measurements from hardware a customer would actually buy.
This is the first milestone that gets to design against real numbers instead of estimating
them, and the numbers decide almost every choice below.

| Situation | Retrieval | |
|---|---|---|
| Typical, idle | **1.2 s** | |
| Five people asking | 2.9 s | |
| **While someone uploads** | **4.5 s** | the case that decides the UI |
| Ten people asking | 5.2 s | practical ceiling |

Plus generation, which on the laptop added **~9 s** and dominates everything.

**One conclusion falls out of that table and drives the whole design: nothing may block on
a complete answer.** A one-second p50 that becomes four-and-a-half seconds the moment a
colleague uploads a contract is precisely the range where a spinner stops reading as
"working" and starts reading as "broken". Streaming is not a nicety here; it is the only
reason the product feels alive on the hardware it is sold for.

---

## 1. Stack

| Choice | Why |
|---|---|
| **React 18 + TypeScript** | mvp.md §5.5; the API is typed and the client should be too |
| **Vite** | dev server and build; no framework runtime to explain to a customer |
| **Tailwind** | one file of design decisions rather than a component library's opinions |
| **No state library** | the server holds the state; see §3 |
| **`pdf.js`** | the citation viewer, §5 |

**No Next.js, and this is not a preference.** This ships as static files served next to the
API inside a customer's network, often with no egress at all. A framework whose good parts
are its server runtime buys nothing here and adds a Node process to a deployment whose
selling point is that it is three containers.

## 2. Streaming chat, and the contract the API already states

`POST /query/stream` emits `token` events and ends with an authoritative `result` event.
The OpenAPI description states the rule the client must honour:

> A client must not present a streamed answer as final until the `result` event arrives,
> and must replace what it displayed if `abstained` is true.

That is not a suggestion the UI may round off. The 0% fabrication gate survives streaming
only because the client honours it, so the component that renders an answer has exactly
three states and the middle one is visually distinct:

| State | Rendering |
|---|---|
| `streaming` | text as it arrives, cursor, **"still writing" affordance** |
| `final` | citations become clickable, answer settles |
| `abstained` | streamed text is **discarded**, abstention shown with documents consulted |

The third transition is the one that matters and the one a careless implementation gets
wrong: it must *replace*, never append. A client that leaves the discarded prose on screen
under an abstention notice has shipped the fabrication the backend spent two milestones
preventing.

**`EventSource` cannot be used.** The browser API is GET-only and sends no headers, and
this endpoint is a POST carrying a bearer token. The client reads the response body with
`fetch` + `ReadableStream` and parses SSE frames itself — about thirty lines, and the same
decision the backend made about parsing SSE rather than taking a dependency.

## 3. No client state library

The server owns everything: the corpus, the answer, the citations, the tenant status. What
the client holds is one in-flight request and its accumulated tokens.

A store would add a synchronisation problem the application does not have. `useState` plus
one `useReducer` for the stream is the whole of it, and when this becomes wrong — history,
multi-conversation, optimistic upload — the right answer is TanStack Query, not Redux.

## 4. What the latency budget forces

1. **Time-to-first-token is the metric the UI is judged on**, not total time. F11 says the
   first token cannot arrive before ~1.2 s of retrieval, so the gap between submit and
   first token is *always* at least a second and the UI must fill it honestly: the question
   echoed immediately, then retrieval acknowledged, then tokens.
2. **Show which phase is running.** The response already carries `took_retrieval_ms` and
   `degraded`. Four and a half seconds during ingestion is tolerable if it says why, and
   intolerable if it is a blank spinner.
3. **`degraded` is surfaced, never swallowed.** When the reranker or the embedder is down
   the answer is still useful and *is measurably worse*. F11 found a configuration where
   this ran silently for as long as nobody looked; the UI is the last place that can make
   it visible.
4. **The upload path warns.** Ingestion quadruples query latency. The upload component says
   so before the file is sent, because a user who knows why it is slow is not a user filing
   a bug.

## 5. Citations are the product

`mvp.md` §2.9: a citation is not the string "page 34" — clicking it opens the PDF **on that
page with the chunk highlighted**. Every layer since F5 has carried normalised bounding
boxes through parsing, chunking, retrieval and generation for a viewer that has not existed
until now.

- `pdf.js` renders the page; boxes are `0..1` relative to page size, so the highlight
  survives zoom and any scaling — which is why they were stored normalised.
- The marker `[2]` in the answer text and the highlight in the viewer are **the same
  object**, driven by `citation.marker`.
- A citation whose document was deleted resolves to a clear "no longer available" rather
  than a broken viewer. F4 made deletion physical and cascading; the client must expect it.

## 6. Tenant status is the first screen

`GET /tenant/status` in one call: counts, `searchable`, `components`, `hardware`.

- `searchable: false` replaces the search box with an explanation. A search box that can
  only return nothing is worse than no search box.
- `documents.failed > 0` is surfaced prominently — a failed document is invisible in
  search, so the status route is the *only* place its absence can be explained.
- `components.reranker: false` is stated plainly, not hidden. On `low-spec` that is the
  configured product, and F11 proved it is the correct configuration for that hardware.

## 7. Scope

**In:** streaming chat with citations, PDF viewer with highlighting, upload with progress,
tenant status, login.

**Out, and recorded:** query history (the tables exist, the UX does not), admin screens for
labels/roles/LLM config (M3), multi-conversation, mobile layouts beyond "does not break",
i18n (§2.14 lists Spanish as a non-goal for the MVP).

## 8. What "done" means for this iteration

1. `make check` green, including the frontend's own type-check and tests.
2. The API client parses SSE from `fetch`, and a test proves an `abstained` result
   **replaces** streamed text rather than appending to it.
3. Upload works against the real API shape.
4. No secret, endpoint or tenant identifier is baked into the bundle: the client is served
   next to the API and talks to a relative path.
