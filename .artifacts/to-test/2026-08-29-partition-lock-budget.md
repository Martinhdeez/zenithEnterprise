# The lock budget, sized so partitioning survives it

**Date:** 2026-08-29 · **Status:** to test · **Branch:** `chore/partition-lock-budget`

Every figure here comes from `backend/eval/lock-budget.json`, written by
`backend/eval/lock_budget.py` against this installation on 2026-08-29. Nothing is quoted
that was not measured, and the two things that were *derived* rather than measured are
labelled as such below.

## What this responds to

`eval/partition-shape.json` established that partitioning `chunks` and `chunk_embeddings`
does not break on planning time. It breaks on **locks**, and it breaks in the worst
direction: a query takes an `AccessShareLock` on every relation of every partition at plan
time, before runtime pruning removes anything, and when the shared lock table runs out
Postgres raises `OutOfMemory` **during planning**. The query never runs. An ordinary search
is a 500, not a slow answer.

That sweep left three numbers unmeasured. This one measures them.

## 1. Nine relations per partition-pair, and it really is nine

`partition-shape.json` projected its lock counts with a hand-written "9 relations", taken
from an index count. Here the scratch pair is built with the installation's actual index
set — read out of `pg_index` rather than typed into the file — and the cost is the **slope**
between two rungs, so the parents and the per-transaction constants cancel:

| partitions | relations in schema | locks for the request | planning | execution |
|---|---|---|---|---|
| 64 | 576 | 585 | 11.3–12.6 ms | 1.4–1.6 ms |
| 256 | 2,304 | 2,313 | 55.5–75.1 ms | 1.9–3.7 ms |

Ranges, because the two passes disagree and both are on disk. Planning is the number that
moves; execution is not.

**Slope: 9.00 locks per partition-pair**, against a schema declaring nine relations — one
heap and five indexes on `chunks`, one heap and two on `chunk_embeddings`. A lock is a
relation, exactly. Two things that were open are closed by it:

- **ParadeDB's `bm25` index is one relation**, not a schema of them. It also propagates from
  a partitioned parent, which was not obvious and which stage 02 depends on.
- **The rest of a search request adds nothing.** `locks_added_by_lexical` and
  `locks_added_by_hydrate` are both **0**. The planner opens every index of both tables for
  the first statement, so the per-*request* budget equals the dense join's, not the sum over
  the three statements of the transaction.

The nine extra locks in the table above are the two partitioned parents and their seven
partitioned indexes: `2313 = 9 x 256 + 9`.

## 2. The setting, verified rather than derived

`max_locks_per_transaction` is not a per-transaction cap. It sizes one table of
`max_locks_per_transaction x (max_connections + max_prepared_transactions)` slots that the
whole cluster draws from, so what it bounds is **concurrency**, and the boundary is not
sharp — the lock hash table grows into shared memory nobody reserved.

Measured at 256 partitions, at both settings, on the same schema. Every transaction on the
ladder held 2,313 locks:

| concurrent | at 64 (6,400 nominal slots) | at 2,560 (256,000 slots) |
|---|---|---|
| 8 | 8 ok | 8 ok |
| 12 | 12 ok | 12 ok |
| 16 | **7 ok, 9 failed** | 16 ok |
| 20 | 12 ok, 8 failed | 20 ok |
| 24 | 3 ok, 21 failed | 24 ok |
| 32 | 8 ok, 24 failed | **32 ok** |

**Bar 1 is met: the sized value passes 32 concurrent and the default is shown to fail the
same test on the same schema.**

The default column is not monotonic, and that is the finding rather than noise in it. More
transactions survived at 20 than at 16. Twelve concurrent held 27,756 locks against a
nominal 6,400 — **4.3x the nominal table** — and every one of them succeeded, because the
lock hash table grows into shared memory nobody reserved. That surplus is real, transient,
and shared with every other backend; the report's `cluster_at_measurement` records that two
other sweeps were on this database at the time. **Plan against the nominal number.**

The shipped value is **2,560**:

- `connection_bounded` — 2,313 measured locks per request. The `max_connections` term
  cancels, because slots are allocated per connection: a value that covers one backend
  covers all one hundred. Rounded up to 2,560 = 10 x 256, which buys one more index per
  partition-pair before the number has to be revisited. At 32 concurrent it charged 74,016
  of 256,000 slots.
- `pool_bounded` would be **463** — the pools cap concurrency at 20 with `max_overflow=0`.
  It is cheaper and it is silently wrong the day somebody raises `api_pool_size`.

**Memory cost, measured:** `shared_memory_size` moves **143 MB → 262 MB** across the
restart, and back to 143 MB when the setting is reset. **+119 MB**, about 500 bytes a slot,
allocated at startup whether or not anything is partitioned.

`shared_buffers` and `work_mem` are left alone. Nothing measured asks for them: what the
ladder moves is planning, from about 0.2 ms unpartitioned to 55–75 ms at modulus 256, while
execution stays under 4 ms at every rung. Buffers and sort memory are not what a planner
walking 256 subplans is short of.

## 3. The prepared-statement path: the risk is the opposite of the one expected

`partition-shape.json` recorded `plan_cache_mode = auto` switching to a generic plan on the
sixth execution and costing 37x. The concern was that stage 02's modulus sits between the
rung where that switch was catastrophic and the rung where it was a large win.

**It does not happen to the product.** Measured through the application engine, at 256
partitions and unpartitioned, with Postgres 17's own counters:

- psycopg **does** promote the statement to a server-side `PREPARE`, on the sixth execution
  of a pooled connection. `pg_prepared_statements` says so.
- Over eleven further executions Postgres built **11 custom plans and 0 generic plans**, in
  both shapes. Round-trip latency: 0.87x partitioned, 1.14x live — no regression.
- `parameter_types` reads `{halfvec,text,text,smallint}`. Although `search.py` passes
  `str(embedding)`, the placeholder sits inside `CAST(:embedding AS halfvec(1024))`, so the
  server infers the parameter's type from the cast context and psycopg sends it untyped.
  There is no per-row text parsing for a generic plan to repeat.

The 37x was an artefact of a harness declaring the parameter as `text`. This sweep keeps
that arm — it fails its bar at 2.9x — precisely so the two can be compared, and it says in
the file which one is the product.

**What this costs instead** is the saving that never arrives: the planning cost of a
256-partition `Append` is paid on **every** execution rather than once per connection.
55–75 ms per dense query at modulus 256, against 11–13 ms at 64 and about 0.2 ms today. The
whole request's median goes 3.1 ms → 24–32 ms across the same two rungs.

## What `zenith diagnose` now reports

A `lock budget` check, beside `bypass surface` and for the same reason: what a schema
declares about partitioning and what a server was started with are independent questions,
and only one of them causes a 500. It counts partitions and their relations out of
`pg_class` on the running installation — no constant in the source — and compares the slot
table against what the pools can demand. `demo-check.sh` reads it as a failure, because
`max_locks_per_transaction` needs a restart and is not fixable between slides.

On this installation, today:

```
OK   lock budget   max_locks_per_transaction=64 x 100 = 6400 slots;
                   no partitioned tables, so nothing draws on them yet
```

Nothing is partitioned yet, so the honest answer is that the setting is not load-bearing —
and it is *said*, so an operator can tell "nothing to size for" from "nobody looked". The
failing branch is covered by tests that build a partitioned table and drop it.

## What stage 02 has to carry

1. **A unique constraint on a partitioned table must include every partitioning column.**
   `pk_chunks (id)` and `pk_chunk_embeddings (chunk_id, embedding_model, embedding_version)`
   are both rejected under `PARTITION BY HASH (tenant_id)`. Both primary keys have to gain
   `tenant_id`. This is not optional and it is not a migration detail — it widens the key of
   the two largest tables.
2. **The setting is a function of the modulus.** 2,560 covers 256. A later doubling to 512
   needs 4,608 and another restart, and the memory doubles with it. `zenith diagnose` will
   say so; nothing else will.
3. **Planning is now the per-query cost.** 55–75 ms at modulus 256, and the prepared-statement
   path does not amortise it. Against a 10 s `statement_timeout` that is not a failure, but
   it is a 5x increase in dense-stage latency that no lock setting removes.
4. **The bm25 index survives partitioning structurally** — one relation per partition,
   propagated from the parent. Whether ParadeDB's *custom scan* still executes on a
   partitioned table is a separate question, listed as open in ADR 0009, and this sweep does
   not answer it.

## To test

- `make check` green on the branch.
- The setting reaches a running installation only when the `db` container is **recreated**
  from `docker/docker-compose.yml`; a restart is not enough. Verify with
  `SELECT setting, source FROM pg_settings WHERE name = 'max_locks_per_transaction'` —
  `source` must read `command line`.
- Re-run `python -m eval lock-budget` after stage 02 lands, against the real partitioned
  schema rather than a scratch copy of it.
