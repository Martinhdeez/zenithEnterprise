# F10 — The product API surface, and the honest cost of streaming

Nine features built a working system. F10 makes it a thing a client can be written
against: documented, predictable under failure, and answering the question every UI asks
first — *what state is this tenant in?*

Four routes are in scope: **upload**, **tenant status**, **search**, and **streaming RAG
chat**. Three of them exist and need hardening. The fourth is new, and it forces a decision
F8 deferred on purpose.

---

## 1. Streaming, and the rule it breaks

F8 recorded this deferral precisely:

> `Completion` is a single response today; the citation binder needs the whole text before
> it can strip an invalid marker, so streaming means shipping tokens the validator has not
> seen yet.

That is still true, and it is not a plumbing problem. The 0% fabrication gate is enforced
*after* the model speaks, by two rules:

1. a marker naming a passage that was not sent is **stripped from the text**
2. an answer with **no valid citation** is discarded and replaced by the abstention

Streaming is compatible with the first and not with the second, and pretending otherwise
would be the quiet kind of failure this project keeps refusing.

### Rule 1 survives, via a marker-aware buffer

A citation marker is short and self-delimiting. The stream holds back any text from `[`
until the matching `]` arrives, validates the number against the shortlist, and then emits
the marker or drops it:

```
tokens in:   "The rate is 1.45% [9] and it applies [2]."
                                 └── buffered, out of range, dropped
tokens out:  "The rate is 1.45% and it applies [2]."
```

The delay is bounded by the length of one marker — single-digit milliseconds of text, not a
perceptible pause. **No invalid marker ever reaches the client**, streaming or not.

### Rule 2 does not survive, and the client is told so

Whether an answer has *any* valid citation is knowable only at the end. By then the prose
has been sent. There are three options and only one is honest:

| Option | Verdict |
|---|---|
| Buffer everything, "stream" it at the end | fake — no time-to-first-token, the entire point |
| Stream, and silently keep an uncited answer | breaks the gate this product is sold on |
| **Stream, and end with an authoritative verdict** | the cost is visible, so it can be handled |

The stream ends with a `result` event carrying the final answer, the citations and
`abstained`. **A client must not present a streamed answer as final until that event
arrives**, and if `abstained` is true it must replace what it showed. This is written into
the endpoint's own OpenAPI description, not just here, because the requirement lands on
whoever writes the client.

**Streaming is opt-in.** `POST /query` keeps the strict behaviour — nothing is emitted
until the answer has passed binding — and remains the default. `POST /query/stream` trades
a weaker guarantee for time-to-first-token, and the trade is named in both places.

### Server-Sent Events, not WebSockets

One direction, text, over plain HTTP. SSE survives corporate proxies that break WebSocket
upgrades, needs no second protocol in the deployment, and reconnects by itself. An
on-premise product is installed behind infrastructure nobody warned us about, and the
protocol that needs the least from that infrastructure wins.

Event types: `token`, `result`, `error`. Named events rather than bare data lines, so a
client can ignore what it does not understand and a fourth type can be added later without
breaking it.

## 2. Tenant status: the question every UI asks first

A client opening the app needs to know whether there is anything to search, whether
ingestion is still running, and which components are degraded — today that takes three
calls and a guess.

`GET /tenant/status` answers it in one:

```json
{
  "documents": {"ready": 39, "processing": 0, "failed": 1},
  "chunks": 19533,
  "hardware": "cpu",
  "components": {"embeddings": true, "reranker": true, "generation": false},
  "searchable": true
}
```

Two deliberate choices.

**`components` reports configuration, not liveness.** It says what this installation is
*supposed* to have, not what answered a ping thirty seconds ago. A status endpoint that
probes three services turns one page load into three network round trips and becomes the
slowest route in the product — and it would still be stale by the time it rendered.
Liveness is what `degraded` on a real answer is for: it is measured on the request that
actually needed the component.

**`searchable` is computed, not stored.** It is `chunks > 0`, which is the only thing that
decides whether search can return anything. A UI that has to derive that itself will derive
it differently in three places.

Counts come from the tenant's own session, so RLS scopes them. No bypass.

## 3. Hardening the three routes that exist

The gap is not correctness — it is that the OpenAPI document does not describe failure, so
a generated client has no types for the half of the API that matters.

1. **Declared error responses.** Every route lists the statuses it can return with the
   `{code, message}` shape. The shape has been stable since F0 and appears nowhere in the
   schema.
2. **`operation_id` on every route**, so generated clients get `uploadDocument` rather than
   `upload_documents_post`. Renaming a Python function currently renames a client method.
3. **Summaries and tags** that describe the product rather than the handler.
4. **Upload limits in the schema**, not only in the code: the 100 MB ceiling and the
   PDF-only rule are contract, and a client should not learn them from a 413.

## 4. What "done" means

1. `make check` green.
2. `GET /tenant/status` returns the counts under RLS, asserted from a second tenant.
3. `POST /query/stream` emits `token` events, then a `result` event, and **an invalid
   marker never appears in any `token` event** — asserted with a model that emits one.
4. Every route declares its error responses and an `operation_id`.
5. No new RLS bypass.

## 5. Recorded as out of scope

- **Rate limits.** Still M4's. Streaming makes `/query` cheaper to hold open and more
  expensive to abuse, which strengthens the case, not the schedule.
- **Reconnection with `Last-Event-ID`.** SSE supports resuming a stream; doing it properly
  means storing partial answers server-side, and a half-generated answer is not something
  this system should persist.
- **Client SDK generation.** F10 makes the document good enough to generate from. Actually
  generating and publishing one is a distribution decision, not an API one.

## 6. Lessons carried in from F9

Two operational patterns worth stating where they will be read, both learned by measurement
rather than reasoning:

**A `None` default that means "build the default" is a trap.** `SearchService(reranker=None)`
does not disable the reranker — it constructs one, which then fails to reach TEI and
degrades. An evaluation measured 67.7% instead of 90.3% that way. Any new optional
dependency in F10 takes an explicit sentinel or a profile flag, never `None` overloaded to
mean two things.

**Prompt rules regress each other.** Adding "be brief" and "start with the answer" together
compressed a good answer to `Yes [1][2][3][5].` Each rule in `prompt.py` now has a test
naming the failure it was written for, so the next person to tidy the prompt cannot silently
delete the load-bearing sentence.
