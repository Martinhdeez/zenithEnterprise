# ADR 0006 — Optional components degrade visibly, and stop being paid for

**Status:** Accepted

## Context

The reranker is the first genuinely optional component. A search without it is still a good
search — it was the entire product one milestone earlier — so its failure must not take a
working product away from a customer.

But F11 measured what "degrade gracefully" cost in practice:

```
reranking off        1,152 ms
reranking failing    6,237 ms   ← same answer, five seconds later
```

The timeout protects the answer's correctness and does nothing for its latency. On a
misconfigured install, that is every query for as long as nobody notices.

## Decision

Three states, not two.

**Configured off** (`low-spec`) is `degraded: false`. The customer chose that profile and
`zenith diagnose` names it; reporting it as a failure would make `degraded` cry wolf.

**Configured but unreachable** is `degraded: true` with a reason.

**Repeatedly unreachable** opens a circuit breaker: three consecutive failures and the
reranker is skipped outright, with one probe let through after sixty seconds.

## Consequences

- **Three failures, not one.** A single timeout is a hiccup — a cold model, a slow batch, a
  GC pause — and tripping on it disables a working reranker for every user for a minute.
- **Half-open is a real state**, not a boolean. Collapsing it either retries on every
  request (the tax this removes) or never retries (one bad minute becomes an outage needing
  a restart).
- **The answer still says `degraded` while the circuit is open.** Skipping the reranker is
  not the same as not having one — the customer paid for better ordering and is getting the
  fused order — and a breaker that hid that would convert a visible failure into a silent
  one, which is the trade this project refuses everywhere else.
- Per-process and in memory. A shared breaker needs Redis to solve a problem measured in
  seconds; each worker discovers the outage within a few of its own requests.
- The breaker is process-wide state, so `conftest` resets it between tests — the same
  reasoning as the login limiter, and without it a test that deliberately fails the reranker
  silently changes the behaviour of whatever runs next.
