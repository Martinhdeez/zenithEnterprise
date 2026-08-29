# The queries that cannot prune

**Status:** measured; two fixes landed here, three handed to stage 02 and shipped by it in 0026
**Evidence:** `backend/eval/unpruned-queries.json`, `backend/eval/unpruned-plpgsql-pruning.sql`
**Gate:** `backend/tests/integration/test_unpruned_query_audit.py`

ADR 0009 partitions `chunks` and `chunk_embeddings` by `tenant_id`, HASH. Every statement
carrying a tenant prunes to one partition and gets faster; `partition-rls-policy-pruning.sql`
proves that survives the real policy, the real role and a per-partition copy of the predicate.

**Every figure in this document was measured at MODULUS 256**, which is what 0026 hard-coded
when the ladder was run. The modulus is `ZENITH_PARTITION_MODULUS` now and defaults to 128 —
see `docs/partitioning-modulus.md` for why — so the lock counts here are twice the shipped
default's and the planning times are worse than twice. They are left as measured rather than
halved on paper: what they are evidence for is the *shape* of an unpruned statement, which is
`modulus` scans against one, and that does not change with the modulus.

This document is about the other half of the schema. Every figure below comes from a run
written to disk under `backend/eval/`, and none of it was typed from memory.

**Read the structural numbers, not the milliseconds.** The ladder was run four times on a
developer machine that two other measurement branches were also using. Partitions scanned,
partitions opened for writing and lock counts were **identical in every run**; the latencies
moved by up to 20%. The argument here rests on the first three, and the timings are quoted
from the run currently on disk so that they can be re-derived rather than believed.

---

## The one paragraph that changes stage 02's design

**A read prunes on `zenith_current_tenant()`. A write does not.**

`UPDATE` and `DELETE` on a partitioned table choose their result relations at *plan* time, and
plan-time pruning needs a constant. `zenith_current_tenant()` is `STABLE`, so it is not one.
The RLS policy is `tenant_id = zenith_current_tenant()`, which means **every write this
product makes to `chunks` scans one partition and opens all 256 for writing**, taking a lock
on each and on each of its five indexes.

Measured at MODULUS 256, on the same statement, differing only in where the tenant comes from:

| ingestion's `_clear_previous` | partitions scanned | partitions opened for writing | locks |
|---|---|---|---|
| tenant from the policy only (`STABLE`) | 1 | 256 | **2,059** |
| tenant as a bound parameter | 1 | 1 | **19** |

6,400 lock slots exist on this installation. 2,059 is a third of them, held for the length of
the transaction, by one ingestion. **Three concurrent ingestions exhaust the lock table and
the fourth fails during planning with `OutOfMemory` — a 500, not a slow answer.**

This is not fixed by adding a tenant predicate. It is fixed by adding a tenant predicate
*whose value is a parameter*. The two forms look identical in review and differ by a hundred
times in lock footprint.

---

## The surface

Enumerated by grepping the three bypass factories CLAUDE.md keeps greppable — `owner_session`,
`platform_session`, `unscoped_session` — and then by reading every `SECURITY DEFINER` function
in the schema, which is the class no grep finds. Six sites survived; four are real and two are
not.

### 1. `zenith_lexical_search` — the expensive one, and the only one on a hot path

Migration 0022. `SECURITY DEFINER`, so no policy applies. Its tenant clause is
`paradedb.term('tenant_id', zenith_current_tenant())` **inside** the `@@@` operand, and a
Tantivy operand is not a partition-key qualifier. Nothing prunes.

| MODULUS | partitions scanned | planning | execution | median | locks |
|---|---|---|---|---|---|
| unpartitioned | 1 | 0.25 ms | 0.41 ms | 0.70 ms | 7 |
| 64 | 64 | 9.50 ms | 4.49 ms | 11.67 ms | 390 |
| 256 | 256 | **81.50 ms** | 19.47 ms | 56.79 ms | **1,542** |

The cost is overwhelmingly **planning**, not execution: the planner builds paths for 256
partitions and five indexes on each before the executor discards 255 of them.

Adding a redundant SQL qualifier beside the Tantivy term takes execution to 0.39 ms and leaves
planning at 52.73 ms, because a `STABLE` key still cannot prune at plan time. **Getting the
planning back needs the tenant to be a parameter**, and `unpruned-plpgsql-pruning.sql` measures
the form that is safe:

```sql
DECLARE v_tenant uuid := zenith_current_tenant();
...
WHERE c.tenant_id = v_tenant
  AND c.id @@@ paradedb.boolean(must => ARRAY[..., paradedb.term('tenant_id', v_tenant), ...])
```

A plpgsql local becomes a parameter of the statement beneath it, so the planner may fold it.
Measured at MODULUS 32: 32 partitions planned and 3.341 ms becomes 1 partition and 0.528 ms,
still 1 and 0.259 ms after nine executions — past the threshold where Postgres would consider
a generic plan.

Three things were checked together, because any one failing makes the change wrong:

- it prunes;
- **the ParadeDB custom scan still runs** — ADR 0002's F18 is a plan that stops running the
  custom scan, returns `NULL` from every `paradedb.score(id)`, ranks everything equally and
  keeps answering;
- **no score is NULL**, asked directly rather than inferred from the plan.

All three hold. The two arms return the same 50 passages with **0 rows at a different rank**.
Scores differ by 0.000295: ParadeDB pushes the SQL qualifier down into the Tantivy query as
one more `must` clause, which adds the same constant to every passage in the tenant. Fusion
downstream is RRF and reads ranks.

**A tenant *parameter* on the function signature would be a leak** and is the tempting version
of this fix. The function runs as its owner; its tenant clause is the only thing between one
customer and another's passages, and a caller who may pass the tenant may pass somebody
else's. The value must keep coming from the GUC.

### 2. `zenith_sync_chunk_labels` — no tenant at all

Migration 0003. `SECURITY DEFINER`. `UPDATE chunks SET label_ids = NEW.label_ids WHERE
document_id = NEW.id`. Fires once per document whose labels change, and once per document
during a purge.

| MODULUS | scanned | opened for writing | locks |
|---|---|---|---|
| unpartitioned | 1 | 1 | 10 |
| 256 | 256 | 256 | **1,546** |
| 256, with `AND tenant_id = NEW.tenant_id` | 1 | 1 | **16** |

The trigger is on `documents`, so `NEW.tenant_id` is already in hand, and a plpgsql field
reference is a parameter. One column on a `WHERE` clause, 1,546 locks to 16.

### 3. The purge cascade — a constraint, not a statement

`purge.py` deletes `documents` scoped perfectly to one tenant. It reaches `chunks` through
`chunks.document_id → documents.id`, and a referential action is a statement Postgres composes
from the constraint's columns. `tenant_id` is not among them, so the cascade cannot prune
whatever the statement above it looked like.

| MODULUS | locks, `documents (id)` | locks, `documents (id, tenant_id)` |
|---|---|---|
| unpartitioned | 15 | 15 |
| 64 | 400 | 22 |
| 256 | **1,552** | **22** |

A purge holds its locks for its whole duration, which is minutes on a real corpus. Today that
would be a quarter of the installation's lock table, for minutes, while everything else is
serving.

The composite key is cheap to add because **partitioning forces the neighbouring change
anyway**: a partitioned table's unique constraints must contain the partition key, so
`chunks (id)` becomes `chunks (id, tenant_id)` and every foreign key referencing it —
`chunk_embeddings.chunk_id` and `query_citations.chunk_id` — becomes composite. Adding
`documents (id, tenant_id)` in the same migration is one more unique constraint.

Note the corollary, measured: with a composite `chunk_embeddings (chunk_id, tenant_id)` key,
the `chunks → chunk_embeddings` cascade **prunes for free**. The forced change pays for
itself.

### 4. Ingestion's `_clear_previous` — the general case

Covered by the paragraph at the top. It is listed separately because it is not a special case:
it is what *every* policy-scoped write to `chunks` will do, and it is the write path this
product runs most often. **Fixed on this branch** — the tenant is now bound from
`TenantContext`, which had it all along.

### 5. `zenith diagnose`'s content check — real, and the cheapest to fix

`app/core/diagnostics.py` `_content`, on `owner_session`, counting all five tables with no
tenant. At MODULUS 256 the two partitioned counts scan every partition: 1,542 and 514 locks,
14.54 ms and 2.47 ms.

The latency does not matter — it runs once per `zenith diagnose`. **The locks do**: an
operator running a diagnostic while the installation is serving should not take a quarter of
the lock table to answer a question nobody needs to the row.

**Fixed on this branch.** The two partitioned tables are estimated from `pg_class.reltuples`,
summed recursively over the partition tree, which scans nothing and takes **7 locks at any
modulus**. The output marks them `~`.

The error it costs is measured twice, because one of the two answers is flattering and useless.
Against the scratch schema, `ANALYZE`d moments before: **0 at every rung**. Against **this
installation's own tables**, last analysed at 17:05 with ingestions since: **13,295 against
13,549, an error of −254, or 1.87%** — the same on both tables. That is the number an operator
will actually see, it is why the output says `~`, and it is what the exact count was buying.
Whether 1.87% is worth 1,535 locks is the trade; this document's position is that it is,
because nobody reads a corpus count to the row and everybody shares the lock table.

### 6. Things on the list that are not problems

Said explicitly, because inventing work to look thorough is worse than a short report.

- **`/system` routes.** `SystemService.organisations` counts `users` and `documents` and
  never touches `chunks` or `chunk_embeddings`. Neither of those tables is partitioned.
  Nothing to do.
- **`requeue.py` and the CLI.** `find_stranded` reads `documents` and `procrastinate_jobs`.
  No CLI command touches either partitioned table. Nothing to do.
- **`tenancy/status.py`'s `SELECT count(*) FROM chunks`.** Runs inside `tenant_session`, so
  the policy supplies the tenant and it prunes — measured at 1 partition scanned, 19.30 ms at
  MODULUS 256 against 0.60 ms unpartitioned. It is on the first screen of every page load, so
  the 19 ms is worth knowing, but it is planning time on a *read* and it is stage 02's
  lock-budget question, not an unpruned query.
- **`purge.py`'s mention of `chunks`.** In a comment, explaining why it does *not* delete from
  it. The gate parses string literals rather than grepping, so this does not fire.

---

## What this branch changed, and what it did not

**Changed**, because they are Python and need no migration:

- `diagnostics._content` counts the two partitioned tables from the catalogue.
- `pipeline._clear_previous` binds the tenant it already has.

**Not changed**, and handed to stage 02 with measurements rather than opinions:

- `zenith_lexical_search` and `zenith_sync_chunk_labels` need `CREATE OR REPLACE FUNCTION`,
  which needs a migration, which needs an Alembic revision number this branch was not
  assigned. Writing one here would be dead code today — nothing is partitioned yet — and would
  race stage 02's own migration for the next free number, which is the two-heads failure
  `CONTRIBUTING.md` describes.
- The composite foreign keys are part of the partitioning migration itself and cannot
  sensibly precede it.

`backend/tests/integration/test_unpruned_query_audit.py` held all five to a list, in the shape
of `test_security_definer_audit.py`. It goes red when a sixth appears, and its author has to
write down which entry it is and why.

**Stage 02 shipped all five in migration 0026, and the list is now one entry long.** Both
functions were rewritten in place — same signature, same bypass, `CREATE OR REPLACE` so 0023's
revoke from `PUBLIC` survived — and the three foreign keys became composite rather than being
dropped. The gate went red on the merge saying so, which is the behaviour this section was
written to produce: it reported the five as *fixed*, not as missing, and nothing new had
appeared beside them. Which mechanism retired each one, and what was read out of `pg_proc` and
`pg_constraint` to confirm it, is in that file's module docstring — recorded there rather than
here because that is the file a reviewer opens when the gate next goes red.

The two paragraphs above about what needed a migration are kept as written. They are the
reasoning that was correct at the time, and a document that edits its own history to look
prescient is one nobody can learn from.

---

## One thing that happened while measuring, and is worth more than a table

The ladder was run four times. The third run failed **in its own teardown**, with
`OutOfMemory: out of shared memory`, and left 2,075 relations behind. An earlier attempt had
its *unpartitioned control* — a statement holding ten locks — fail the same way.

Neither failure was caused by anything this branch did. Two other measurement branches were
building partitioned schemas on the same database at the same time, and
`max_locks_per_transaction * max_connections` sizes **one table the whole cluster draws from**.
A neighbour took it, and a ten-lock statement failed.

That is the entire argument of this document, observed from the wrong end. The lock cost of an
unpruned statement is not a property of that statement — it is a claim on a shared resource,
and what it breaks is whatever else is running. A single-query benchmark cannot see it, which
is why every arm here reports a lock count beside its latency.

The harness now drops partitions sixteen at a time with a one-at-a-time fallback, because a
teardown that gives up under exactly the pressure it is measuring leaves the schema behind.

---

## The general rule, for whoever writes stage 02

Two sentences worth more than the tables above.

**On a read, the policy is enough.** `zenith_current_tenant()` prunes at executor startup and
the plan says `Subplans Removed`.

**On a write, the policy is never enough.** Bind the tenant explicitly, from the context that
already holds it, in every `UPDATE` and `DELETE` against a partitioned table — and put it in
every foreign key that reaches one, so the cascades Postgres writes carry it too.
