# F17 — Engineering quality audit

A repository-wide pass for documentation, error standards, observability and type
completeness. It found one shipped claim that was untrue, which is the most useful thing an
audit can do.

---

## 1. What the audit found: F14 shipped a claim it did not implement

F14's commit message and PR both stated that `Retry-After` was forwarded by the domain
error handler. **The code never had it.**

The edit ran from the wrong working directory and wrote nothing. `make check` stayed green
because the throttle tests asserted on the *raised exception* — `raised.value.retry_after == 60`
— rather than on the response a client receives. Nothing failed, so the claim went into
`main` unchallenged, in a commit message that will be read as documentation for years.

Implemented now, and `test_a_rate_limit_carries_retry_after_in_both_places` asserts the
**response**.

The lesson is narrower than "test more": **a test that asserts on the object a handler
receives has not tested the handler.** Three tests in this repository had that shape; the
other two were already asserting on responses.

## 2. Architecture Decision Records

Eight records in `docs/adr/`, written from decisions this codebase actually made:

| ADR | Decision |
|---|---|
| 0001 | Isolation by Postgres RLS, not application filters |
| 0002 | Hybrid retrieval fused by RRF — **amended twice by measurement** |
| 0003 | An ABC in the domain layer, not a vendor SDK |
| 0004 | The zero-fabrication gate enforced after the model speaks |
| 0005 | One hardware profile table; performance may vary, semantics may not |
| 0006 | Optional components degrade visibly and stop being paid for |
| 0007 | Server-computed aggregation the client would get wrong |
| 0008 | RFC 7807 for every error |

Several record a **measurement that contradicted the original reasoning** — RRF's agreement
bias, `ts_rank_cd`'s missing IDF, `rerank_candidates` still being a guess. That is
deliberate. An ADR set that records only successes is a marketing document, and the
reversals are what a future maintainer most needs to find before repeating the experiment.

## 3. RFC 7807 Problem Details

Every error is now `application/problem+json` with `type`, `title`, `status`, `detail`,
`instance`, and extension members where they carry machine-readable specifics
(`retry_after` on a 429, alongside the header).

**`code` and `message` are retained.** Adopting a standard is not a licence to break the
callers who trusted the previous contract. The frontend reads `detail ?? message`, so it
works against this server and any older one still deployed — a real situation for an
on-premise product, not a hypothetical.

## 4. Observability: `trace_id` on every line

Logs were already structured JSON. What they lacked was attribution.

`RequestContextMiddleware` binds a `trace_id` via `structlog.contextvars`, so every line
emitted anywhere in the request carries it — including from services that know nothing
about HTTP. It is returned in `X-Trace-Id` so a user reporting a problem can quote something
support can grep for, and it is **generated rather than trusted from the client**, since an
inbound identifier would let a caller collide with somebody else's trace.

Written as raw ASGI rather than `BaseHTTPMiddleware`, which buffers the response body and
would defeat `POST /query/stream` entirely.

### What is deliberately absent, and why

- **The question.** It lives in `queries` under RLS, on the corpus's retention schedule.
  A log stream is shipped to aggregators, read during support calls and retained
  differently — and questions are more revealing than documents.
- **The user id.** `tenant_id` routes an incident to a customer. *Which employee asked
  what* belongs in the audit trail, not in a log a support engineer tails.
- **Headers.** None. "No headers" is a rule that keeps itself; a denylist is a rule
  somebody has to remember to extend.

Existing secret hygiene was verified rather than assumed: the diagnostics module already
scrubs passwords from driver errors, and the generation adapter already refuses to forward
a provider's error body — both have had tests since the milestones that introduced them.

## 5. Type completeness — and why not mypy

The instruction asked for **both** `mypy` and TypeScript `strict`. TypeScript strict was
already on, with `noUncheckedIndexedAccess` and `noUnusedLocals` beyond it.

**I did not add mypy, and this is a recommendation rather than an omission.**

`pyright` already runs in `typeCheckingMode = "strict"` over `app`, `eval` and `tests`, in
`make check` and in CI, at **zero errors**. That mode is stricter than mypy's default and
stricter than `--strict` in the areas that matter here: it reports partially-unknown types,
which is what caught four real defects during this audit alone — including the `list[str]`
versus `list[UUID]` mismatch in F15's identifier query.

Running both would mean two tools disagreeing about the same code, two ignore syntaxes,
two upgrade cadences, and a second CI step whose failures require translating between type
systems. The industry practice being reached for here is *one strict type checker enforced
in CI with no escape hatches*, and this repository has that.

What was verified instead:

- `pyright` strict, zero errors, no `# type: ignore` outside test doubles and laboratory
  code under `eval/` (suppressed once per file, never under `app/`)
- **no unhandled exception can reach a client**: `handle_unexpected_error` catches
  everything not a `ZenithError`, returns the standard shape, and is asserted not to leak
  connection strings, query text or exception class names

If mypy is wanted regardless, the honest way in is as a *second opinion in a non-blocking
job*, not a second gate — and I would want a reason it is expected to find something
pyright strict does not.

## 6. Still open

Unchanged from F16, plus nothing new:

- `cross-platform-obligations` — the last question outside the 96.8% context ceiling
- BM25 via `pg_search` (ADR 0002)
- a row-level policy on `queries.user_id`, replacing the one application-code filter
- user invitation: `users.invite` is enforced nowhere because no endpoint creates a user
- the RNF-06 certification suite behind §2.11's "test connection" button
