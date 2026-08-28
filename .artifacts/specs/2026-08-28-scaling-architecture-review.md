# Scaling architecture review — eleven findings, ranked by measurement

**Date:** 2026-08-28
**Branch:** `perf/scaling-review`
**Installation measured:** the local stack, `alembic current == head == 0022`, profile `cpu`,
two tenants, 13,549 embeddings in one HNSW graph.
**Report produced for this review:** `backend/eval/tenant-scale.json`, written by
`backend/eval/tenant_scale.py` (`docker compose exec -T api python -m eval tenant-scale`).

Every figure below comes from a run written to disk under `backend/eval/`, or from a
measurement already committed there or in a migration. Nothing is quoted that was not
produced. Where a number is an estimate rather than a measurement it says so.

---

## The one measurement that changed the order

The review arrived with eleven findings and a suspicion about the second one. The suspicion
was right, and it is worse than it was stated, so it now sits at the top and it drags two
other findings up with it.

**The dense half of retrieval is losing candidates, silently, on this installation, today, at
two tenants.** Not at a hundred. Two.

At the shipped configuration — `CANDIDATES = 50`, `hnsw_ef_search = 100`, no
`hnsw.iterative_scan` — the dense half asks for 50 passages and gets:

| Scope | Accessible | Share of graph | Plan | Dense candidates returned | Dense recall | Questions given **nothing** |
|---|---:|---:|---|---:|---:|---:|
| Tenant-wide (M0 baseline) | 8,273 | 61.1% | HNSW | **43.1 / 50** | **0.842** | **5 of 42** |
| Widest label (Legal & Regulatory) | 7,141 | 52.7% | HNSW | **30.0 / 50** | **0.588** | **10 of 42** |
| Second tenant, tenant-wide | 5,276 | 38.9% | exact | 50 / 50 | 1.000 | 0 |
| Label `legal/contracts` | 1,979 | 14.6% | exact | 50 / 50 | 1.000 | 0 |
| Label `finance/2026/invoices` | 685 | 5.1% | exact | 50 / 50 | 1.000 | 0 |
| Label `Digital` | 70 | 0.5% | exact | 50 / 50 | 1.000 | 0 |

Five questions get **zero** dense candidates while the tenant holds 8,273 passages it is
entitled to read. They are named in the report so the claim can be checked by hand:
`attention-optimizer`, `attention-multihead`, `identifier-omb-number`, `identifier-oj-page`,
`identifier-usc-citation`. The loss is topical rather than random — those are the questions
about transformer papers, and the *other* tenant's corpus is a cyber-security standards
archive whose vectors sit between them and their answers. **The dense half fails hardest
exactly when a neighbouring tenant works in the same field**, which in an enterprise product
sold by vertical is the normal case rather than the unlucky one.

### The hypothesis needs one correction

It was stated as monotonic: as tenants multiply, one tenant's rows become a smaller fraction
of the graph, so fewer survive. The first half is right and the conclusion is not. Below
roughly 15% accessible the planner stops believing the HNSW index is worth it and switches to
an exact scan, and recall returns to 1.000.

So the damage is not a slope, it is a **band**, and the band is the middle:

```
 100% ── exact plan, correct, and O(corpus)          ← narrow labels, small tenants
   |
  ~15% ─────────────── the planner switches here ───────────────
   |
  61% ── HNSW plan, silent truncation, 0.84 recall   ← where this installation sits
```

That is a worse finding than the monotonic version, for two reasons. The safe end is safe
only by a **cost estimate** — the same objection ADR 0002 raises against coaxing the planner
with `enable_bitmapscan = off`, now true of the shipped dense query rather than a rejected
design. And the safe end is safe only while the corpus is small: the exact plan already costs
20.8 ms at 5,276 rows against 1.2 ms for the index, and it grows linearly.

### Nothing said so

`degraded` stayed `false` at every point. The service's own log line, from the end-to-end run
inside this report:

```
search  degraded=False dense=0 exact=0 lexical=50 relevance=confident returned=8 took_ms=891
```

The dense half returned nothing, and the answer went out marked confident and undegraded.

### Why nobody noticed

End-to-end headline Recall@8 is **0.90**, unchanged from `live-recall.json`, with the same
three misses. The lexical half is covering for the dense half at the depth the interface
shows. That is the hybrid working as designed, and it is also the reason a 16% loss of dense
candidates has been invisible for as long as it has existed. It will stop covering when a
question has no exact string to match — which is precisely the class of question the dense
half exists for.

### The two remedies, measured

`hnsw.iterative_scan = relaxed_order` — the setting `search.py:152-156` already uses, but only
when a document scope is passed:

| Scope | Default recall | Iterative recall | Default median | Iterative median |
|---|---:|---:|---:|---:|
| Tenant-wide | 0.842 | **0.964** | 1.18 ms | 1.23 ms |
| Widest label | 0.588 | **0.937** | 1.00 ms | 1.25 ms |

Full 50 rows at every scope, for between 0.05 and 0.25 ms. Note it does not reach 1.000:
`relaxed_order` is approximate by construction, which is the trade `search.py` already
documents and accepts for scoped search.

Raising `hnsw.ef_search` alone, at the tenant-wide scope:

| ef_search | Returned | Dense recall | Questions given nothing | Median | p95 |
|---:|---:|---:|---:|---:|---:|
| 100 (shipped) | 43.1 | 0.842 | 5 | 1.14 ms | 1.35 ms |
| 200 | 46.8 | 0.916 | 2 | 1.43 ms | 1.68 ms |
| 400 | 50.0 | 0.988 | 0 | 2.06 ms | 2.46 ms |
| 800 | 50.0 | 0.996 | 0 | 3.41 ms | 4.09 ms |
| 1000 | 50.0 | **1.000** | 0 | 50.78 ms | 56.68 ms |

It works today and it does not scale: `hnsw.ef_search` is **capped at 1000 by pgvector** and
rejects anything above it, so it is a fixed candidate budget against a graph that grows. At
1000 the cost jumps 25-fold. It is a dial, not a fix.

---

## The eleven findings

Ranked by what the measurement showed, not by the order they were reported in.

---

### 1. The dense half truncates silently against a shared HNSW graph — P0

**Evidence.** `backend/eval/tenant-scale.json`, summarised above. `search.py:152-156` sets
`hnsw.iterative_scan` only when `documents` is passed; the comment says iterative scan "is a
behaviour change to the hot path, and the hot path is not what this is for". The measurement
says the hot path is exactly what it is for: the hot path has the same post-filter problem the
document scope has, arriving from RLS instead of from a `WHERE` clause, and it is unguarded.

**Proposed change.** Set `hnsw.iterative_scan = relaxed_order` on every dense query, not only
scoped ones. One line: hoist the `SET LOCAL` out of the `if documents:` block. Combine it with
the round-trip collapse in finding 7 so it costs no extra statement.

**Risk to the invariants.** None to isolation. Iterative scan changes *how long the index
keeps walking*, never *which rows a policy admits* — every row it returns has already
satisfied the same quals, and `hnsw.max_scan_tuples` bounds the walk. It cannot widen a result
set beyond what the policy allows, because the filter is what it is iterating against.
Invariant 5 is unaffected: more candidates cannot manufacture a citation. The real risk is
latency on a large corpus, and it is bounded by `max_scan_tuples` (20,000 by default here).

**Measurement that proves it worked.** Re-run `python -m eval tenant-scale`: every point
reports `rows_returned_min == min(50, accessible)`, `returned_nothing` empty, and dense recall
above 0.93 at the HNSW-plan scopes. Then `python -m eval live` to confirm headline Recall@8
does not regress from 0.90, and that p95 stays inside the interactive budget.

---

### 2. A silent retrieval degradation has no way to become visible — P0

**Evidence.** `dense=0 ... degraded=False ... relevance=confident`, logged by the shipped
service during this review's own end-to-end run. ADR 0006 is built on the principle that
optional components degrade *visibly*, and ADR 0005 records F11's fifteen-point recall loss
that "no test failed" on. This is the same failure one layer down: the reranker's absence is
detected because a component is unreachable, and a truncated dense half looks exactly like a
healthy one that happened to find fewer neighbours.

**Proposed change.** Make the shortfall observable. `dense()` knows both what it asked for and
what it got; when it returns materially fewer than `limit` rows while the tenant holds more
than `limit` accessible passages, that is a fact worth carrying. Two levels, and the second
needs a product decision:

1. Log it, and add it to `zenith diagnose` — no API change, no risk. Do this now.
2. Surface it as a `degraded` reason. This is a behaviour change to the response contract and
   should be argued separately: `degraded` currently means "a component you paid for is not
   working", and stretching it to "the index gave us less than we asked for" may make it cry
   wolf in exactly the way ADR 0006 refuses for `low-spec`.

**Risk to the invariants.** None. It adds no query, no filter and no bypass; the count it
reports is of rows the caller already received.

**Measurement that proves it worked.** With finding 1 deliberately reverted in a scratch
branch, `python -m eval tenant-scale` must show the shortfall and the log must name it. A test
that stubs a dense result shorter than the limit and asserts the diagnostic fires.

---

### 3. The whole API is capped at ten concurrent database operations — P0

**Evidence.** `config.py:109` (`api_pool_size: int = 10`) and `database.py:74-75`
(`pool_size=pool_size or settings.api_pool_size, max_overflow=0`). Every HTTP request that
touches the database takes one of ten, and holds it for the duration of its transaction. A
search holds one across four sequential statements (finding 7); `GET /status` holds one across
a full scan of `chunks` (finding 8). The eleventh concurrent request waits for a connection
before it does anything at all, and the wait is not covered by `statement_timeout` because no
statement has started.

This has not been measured under load. `vps-contention.json` measures contention between
ingestion and queries for TEI, not for the pool.

**Proposed change.** Two parts, and only the first is safe to guess at.

- Make it configurable and documented against the profile, the way `hnsw_ef_search` is. The
  number should be a row in the hardware profile table, not a constant in `config.py`, because
  ADR 0005's rule is that hardware-dependent numbers live in exactly one table.
- Choose the value by measurement, not by raising it. `max_overflow = 0` is a deliberate
  choice — an overflow pool converts a saturated database into a slower one rather than a
  refusing one — and it should stay. Raising `pool_size` without knowing where Postgres itself
  saturates moves the queue from the application to the database, which is worse because it is
  invisible there.

**Risk to the invariants.** None to isolation: every pooled connection is `zenith_app` and
every session still sets its own context. Invariant 2 is untouched — this changes no factory
and adds no name. The risk is operational: a pool larger than the database's `max_connections`
divided by the number of processes fails at connect time, and the worker has its own pool.

**Measurement that proves it worked.** A new `eval/` capacity run, in the style of
`vps_contention.py`: concurrent searches at 1, 5, 10, 20, 50 in flight, reporting p50/p95/p99,
errors, and time spent waiting for a connection distinct from time spent in Postgres. That
last split is the whole point — without it a saturated pool and a slow query are the same
number. This report does not exist yet and finding 11 is about that.

---

### 4. The lexical half is linear in matches, BM25 is built and switched off, and ADR 0002 says it cannot exist — P0

**Evidence.** Migration `0022_bm25_lexical_search.py`: `ts_rank_cd` scores every matching row
before `LIMIT` can choose, measured at **5,953 ms on 300,000 passages under the real policy**,
against a ~130x faster BM25 path that resolves the top N inside the index. The
`statement_timeout` is 10,000 ms, so the gap between a slow lexical query and a failed one is
under a factor of two.

The implementation shipped: `zenith_lexical_search` exists, the `ix_chunks_bm25` index exists
on this installation, `search.py:185-220` calls it, and `config.py:123` reads
`lexical_engine = "tsvector"`. The switch is off.

`docs/adr/0002-hybrid-retrieval-and-fusion.md` still carries a section titled
**"Not taken — BM25 via `pg_search`, and why it cannot be"**, which states that BM25 "cannot
supply a score under row-level security". Migration 0022 says why that is wrong: the score is
NULL only when the *label clause* is left outside the Tantivy query, and 0022 moves it inside.
A reader who trusts the ADR — which is what an ADR is for — concludes the work is impossible
and does not look for the function that does it.

**Proposed change.** Split into the part that is safe and the part that is not.

- **Safe, done on this branch:** amend ADR 0002 so the section records itself as superseded by
  migration 0022, states what changed, and points at the switch and at what still has to be
  measured before it is flipped. An ADR that is wrong about a decision is worse than no ADR.
- **Not safe, and deliberately not done:** flipping `lexical_engine` to `bm25`. It is a real
  behaviour change to the ranking every recall figure in `eval/` was measured against, and
  0022 itself flags that ParadeDB's `en_stem` tokenizer does not fold accents, so a Spanish
  corpus would change behaviour in a way no current eval question covers. **This needs an eval
  run and a human decision; it is proposed here with evidence and stopped.**

**Risk to the invariants.** The documentation amendment carries none. The flip carries a real
one: `zenith_lexical_search` is `SECURITY DEFINER` and is already counted in the bypass surface
by 0022, which brings that surface to five entries. It takes no tenant or label argument and
reads the same session variables the policies read, so there is no parameter through which
another tenant's corpus can be requested — but flipping the switch makes that function the
lexical half for every query, and the RLS isolation matrix must run against it first.

**Measurement that proves it worked.** For the amendment: none needed beyond review. For the
flip, before anyone considers it: the full RLS isolation suite green against
`zenith_lexical_search`; `python -m eval live` showing headline Recall@8 not below 0.90 and
identifier questions at 100%; a lexical p95 at the claimed corpus size inside the interactive
budget; and a run of the accent-folding case on the Spanish corpus that 0022 warns about.

---

### 5. The dense half's correctness depends on a planner cost estimate — P1

**Evidence.** In `tenant-scale.json`, the plan differs by scope: `hnsw` at 61.1% and 52.7%
accessible, `exact` at 38.9% and below. The exact plan is correct and the HNSW plan is not, and
nothing in the application chose between them. This is the failure mode ADR 0002 explicitly
refuses when it rejects `enable_bitmapscan = off`: "correctness would then depend on a plan
choice, and the failure mode when the plan changes is the silent one". It already does.

**Proposed change.** Finding 1's fix removes the dependence rather than managing it: with
iterative scan on, the HNSW plan returns the full candidate set, so the two plans agree and it
no longer matters which one is chosen. Nothing else should be done here — in particular, no
planner coaxing, for the reason ADR 0002 gives.

**Risk to the invariants.** As finding 1. None.

**Measurement that proves it worked.** `tenant-scale.json`'s `plan_sample` still shows both
plans occurring, and dense recall is above 0.93 at *every* point regardless of which one the
planner picked. Agreement across plans is the property to assert, not a particular plan.

---

### 6. `hnsw.ef_search` is a fixed budget against a growing graph — P1

**Evidence.** The sweep above: 400 restores full recall today at 2.06 ms, and the setting is
capped at 1000 by pgvector, where it costs 50.78 ms median against 1.14 ms at 100. Today's
graph is 13,549 vectors. The lexical finding quotes a 300,000-passage target. The budget does
not move with the corpus.

`ef-search.json` measured this knob at 8,273 embeddings and one tenant and concluded it was
worth nothing end to end — every setting from 100 upward returned identical results. That
conclusion was correct for its corpus and is no longer correct for this one, which is the
clearest available demonstration that a single-tenant measurement does not survive a second
tenant.

**Proposed change.** Do not raise it as the remedy for finding 1; keep `cpu` at 100 and let
iterative scan do the work. Re-derive `ef-search.json` on the current two-tenant graph so the
profile table's value has current evidence behind it rather than superseded evidence, and
record in `hardware.py` that the knob's ceiling is 1000.

**Risk to the invariants.** None. It is a speed/recall trade inside one session, and ADR 0005
already places it in the profile table where hardware-dependent numbers belong.

**Measurement that proves it worked.** A re-run of `python -m eval ef-search`, whose index
sweep now reports `rows_returned` below `CANDIDATES` at the shipped setting — the number that
was 50 at every setting on the single-tenant corpus and is not any more.

---

### 7. Five round trips per request to establish the RLS context — P1

**Evidence.** `database.py:145-165`: four `set_config` calls plus one
`SET LOCAL statement_timeout`, each its own `await session.execute`. Every `tenant_session`
pays all five before its first useful statement. A search then adds a sixth for
`SET LOCAL hnsw.ef_search` inside `dense()` (`search.py:146-150`), then `to_tsquery`, the
lexical query, the dense query, the exact query and `hydrate` — around eleven round trips for
one search, of which six carry no data. A scoped search opens a second `tenant_session` in
`_reachable` and pays the five again.

Each round trip is small and none of them is free: they are serialised on one connection out
of ten (finding 3), so they occupy the scarce resource for their whole duration.

**Proposed change.** Collapse the five into one statement. `set_config` returns a value, so
they compose into a single `SELECT` with the four calls in the target list and the timeout
appended — every setting stays transaction-local (`set_config(..., true)`), the values stay
bound parameters, and the semantics are identical. Applied on this branch.

The remaining reductions are follow-ups, not part of this change: folding
`SET LOCAL hnsw.ef_search` into the same statement (which finding 1 makes attractive, since
it would carry `hnsw.iterative_scan` too), and letting `_reachable` reuse the caller's session
rather than opening a second one.

**Risk to the invariants.** This one touches the RLS context directly, so it is the change in
this document that needs the closest reading. It does not weaken anything: the same four
variables are set, to the same values, with the same `is_local = true`, in the same
transaction, before any query runs. What must not happen is a rewrite that makes them
session-local or that loses a value — a context that forgets `zenith.user_id` reads nothing
rather than everything (the comment in `database.py` says why), and a context that leaks
across pooled connections would be an isolation failure of the first order. Both are covered
by existing tests.

**Measurement that proves it worked.** `make check` green — the RLS and isolation suites are
the assertion here, and they run against real Postgres via testcontainers precisely so that
this kind of change cannot pass on a database without policies. Beyond correctness, the
capacity run from finding 11 should show the saving; four fewer round trips on a path that
holds one of ten connections is worth having, and is not worth claiming a number for until
that run exists.

---

### 8. `GET /status` runs a full scan of `chunks` to compute a boolean — P1

**Evidence.** `features/tenancy/status.py:66`:
`chunks = await session.scalar(text("SELECT count(*) FROM chunks")) or 0`, under RLS, on a
per-request endpoint (`tenancy/router.py:18`). Postgres has no cheap `count(*)`; under the
tenant policy this scans every row the caller can see. Its only structural consumer is
`searchable = int(chunks) > 0`. At the 300,000-passage scale finding 4 is written against,
this is a sequential scan holding one of ten connections on every page load.

**Proposed change.** Compute `searchable` with `EXISTS`, which stops at the first row. Keep the
count if the interface displays it, but source it separately and accept that it is a statistic
rather than a live total — or take it from `pg_class.reltuples` scaled by the tenant's share,
which is what an estimate should look like when it is honestly labelled as one.

**Risk to the invariants.** `EXISTS` runs under the same policy as `count(*)` and sees the same
rows; nothing about isolation changes. Care is needed only if the count is ever sourced from a
catalogue view, because `pg_class` is not tenant-scoped — that variant must not report another
tenant's row count, which means it needs a scaling factor and a clear label, or it should not
be done at all.

**Measurement that proves it worked.** `EXPLAIN ANALYZE` on the endpoint's query showing a
plan that stops early, and the capacity run from finding 11 showing `GET /status` p95 flat as
the corpus grows rather than linear in it.

---

### 9. One global `statement_timeout` for every kind of session — P1

**Evidence.** `config.py:111` (`statement_timeout_ms: int = 10_000`), applied identically in
`set_rls_context` and in `unscoped_session`. An interactive search, a bulk administrative
query and a login lookup get the same ten seconds. Finding 4's measurement puts the lexical
half alone at 5,953 ms at 300,000 passages, which means the interactive budget and the
timeout are within a factor of two of each other on the path most likely to be slow.

**Proposed change.** Give the interactive path its own, shorter budget and leave the
administrative one where it is. The right shape is a profile field, following ADR 0005: a
number that depends on the machine belongs in the profile table, and a timeout that decides
whether a user waits or gets a partial answer is exactly that. The failure it should produce
is the one ADR 0006 already built — return the half that finished and say `degraded` — rather
than a 500.

**Risk to the invariants.** None to isolation. The risk is behavioural: a timeout short enough
to fire on a legitimate slow query converts a working search into a degraded one, so the value
must come from a latency distribution and not from a guess.

**Measurement that proves it worked.** The capacity run from finding 11, reporting the search
latency distribution at the claimed corpus size, with the chosen timeout placed above the p99
rather than picked to look tidy.

---

### 10. Query embedding and ingestion compete for one TEI instance — P1

**Evidence.** `features/retrieval/service.py:1-12` records it and F5's measurement:
**13.4 minutes of TEI held per 100 dense pages** of ingestion, while query embedding is
synchronous and in-request. The mitigation already shipped — a short embedding timeout, and
the lexical half alone with `degraded` set when it expires — which is the correct MVP answer
and is also finding 1's problem in reverse: under ingestion load the product falls back to the
lexical half, and the lexical half is the one that is linear in matches (finding 4).

The two findings interact and the interaction is not measured. Under concurrent ingestion at
300,000 passages, the fallback path is the slow path.

**Proposed change.** Nothing structural for the MVP; a second TEI instance doubles the memory
footprint of a profile defined by not having any, and that trade is already argued in the
module. What is missing is the measurement of the combined case, and a `zenith diagnose` line
that names the contention when it is happening, so an operator watching a slow installation
sees the cause rather than a symptom.

**Risk to the invariants.** None.

**Measurement that proves it worked.** Extend `vps_contention.py` — which already measures
ingestion against queries — to report search p95 and `degraded` rate during ingestion at a
corpus size where the lexical half is slow, not only at the current one.

---

### 11. There is no reproducible capacity report, and the numbers that matter have no script — P2

**Evidence.** CLAUDE.md's own trap list: "every figure quoted anywhere must come from a run
written to disk under `backend/eval/`", and "two stale corpus statistics survived in the
runbook for weeks because nobody re-derived them". The 300,000-passage lexical measurement in
migration 0022 — the number the entire BM25 argument rests on — has no committed generating
script. `ef-search.json` was measured on a one-tenant corpus and its conclusion has been
falsified by a second tenant (finding 6), which nothing detected, because a report carries no
record of the corpus it was measured against.

Findings 3, 7, 8, 9 and 10 all end with "the capacity run would show this", and that run does
not exist.

**Proposed change.** One `eval/` command producing one report, and a header on every report
recording what it was measured against: date, `alembic` revision, hardware profile, container
versions, tenant count, embedding count, corpus size, warm-up policy, sample count. Then
p50/p95/p99, throughput, errors, degradations, and connection-wait separated from query time.
`tenant-scale.json` records the first half of that header already and should be brought to the
full shape when the capacity run is written.

The generating script for the 300,000-passage experiment should be committed or the number
should be re-derived by one that is.

**Risk to the invariants.** None, provided the harness stays read-only in the way
`tenant_scale.py` is: `SET TRANSACTION READ ONLY`, every transaction rolled back, ground truth
taken under the same RLS context as the measurement rather than through a role that bypasses
it. A capacity harness that needs synthetic tenants must create them through the supported
provisioning path and remove them, never by writing rows behind the policies.

**Measurement that proves it worked.** The report exists, and re-running it on an unchanged
installation reproduces its numbers inside a stated tolerance.

---

## What was applied on this branch, and what was not

**Applied**, each as its own commit, both safe and reversible:

- `docs/adr/0002-hybrid-retrieval-and-fusion.md` — the "Not taken — BM25" section records
  itself as superseded by migration 0022 and says what changed (finding 4, documentation half).
- `backend/app/core/database.py` — the five RLS-context round trips collapse into one
  statement, semantics unchanged, settings still transaction-local (finding 7).
- `backend/eval/tenant_scale.py` and `backend/eval/tenant-scale.json` — the measurement this
  review turns on.

**Not applied, and why:**

- **Finding 1's one-line fix** is the most valuable change in this document and it is a
  behaviour change to the hot path of every search. It is proposed with the measurement that
  justifies it and left for a decision.
- **Flipping `lexical_engine` to `bm25`** needs an eval run and a human decision. Stopped
  deliberately.
- **Anything touching RLS, the bypass surface, the citation gate, a schema migration, or
  partitioning `chunks` / `chunk_embeddings`.** None of the eleven findings requires any of
  them, which is worth stating: the largest measured problem here is fixed by one line of
  session configuration, not by architecture.
