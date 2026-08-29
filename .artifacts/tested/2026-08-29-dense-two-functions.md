# Two functions instead of a disjunct: the hypothesis is wrong and the decision survives anyway

Measurement: `backend/eval/dense_two_functions.py`, report `backend/eval/dense-two-functions.json`.
Premise: `backend/eval/dense-plan-time-pruning.sql` section 5f, `backend/eval/dense-plan-time.json`.
Every figure below comes from that report. Three rungs — 32, 128, 256 partitions — over the real
13,549 passages and eight synthetic tenants. **Four consecutive runs produced an identical
verdict table**, so the readings below are reproductions rather than one draw; the timings move a
few hundredths of a millisecond between runs and the plans do not move at all.

**Installation these readings come from:** Postgres 17.5, migration **0026**,
`public.chunks` partitioned into 128, `max_locks_per_transaction` 2,560 over 100 connections —
256,000 cluster lock slots. Recorded per rung in the report's `environment`. An earlier arm taken
before that deploy was discarded rather than mixed in.

---

## The question

Section 5f found that `search.dense()`'s optional document scope, expressed in plpgsql as an
unconditional filter behind a NULL guard — `AND (docs IS NULL OR c.document_id = ANY(docs))` —
loses the HNSW index under a generic plan, and isolated the cause to the disjunct's presence in
the statement's shape rather than to the runtime value of `docs`.

So: do not write a disjunct. Ship two functions, one with no `docs` parameter at all and one whose
`docs` is never NULL, and let `search.py` pick — the way it already picks whether to write the
scope clause.

## The answer: no. The disjunct was not the thing.

`scoped_bare` — the tenant qualifier plus a bare `c.document_id = ANY($6)`, no `OR` anywhere —
loses the HNSW index under a forced generic plan at **all three moduli**, exactly as the
NULL-guard form does:

```
->  Sort  Sort Method: top-N heapsort
      ->  Result
            One-Time Filter: (current_setting('zenith.tenant_id') = $1)
            ->  Hash Join   Hash Cond: (e.chunk_id = c.id)
                  ->  Append   Subplans Removed: 127
                        ->  Seq Scan on e5 e_1  (actual rows=1700)
```

against the unscoped candidate at the same rung, which at modulus 32 keeps it:

```
->  Nested Loop
      ->  Merge Append   Sort Key: ((e.embedding_half <=> $2))   Subplans Removed: 31
            ->  Index Scan using e5_embedding_half_idx on e5 e_1
                  Order By: (embedding_half <=> $2)
```

`p_scoped_bare` and `p_local` differ by the `document_id = ANY($6)` predicate and by nothing else
— same parameters, same positions, same order — so the array predicate on the `chunks` side is
what tips the join strategy away from the ordered index scan. Removing the `OR` bought nothing.
**Bar 2 failed and stays failed in the file.** This is the outcome the hypothesis' author called
most likely, and it is the one that happened.

## Two things the premise did not have, and both change what that failure means

### 1. At the modulus that shipped, every shape fails it together

`shipped_scoped` — the literal statement `search.dense()` sends today when scoped — was never
measured under a forced generic plan by section 5f. Measured here, under a forced generic plan:

| modulus | `today` | `local` (unscoped candidate) | `shipped_scoped` | `scoped_or` | `scoped_bare` |
|---|---|---|---|---|---|
| 32 | no index | **index** | no index | no index | no index |
| **128** | no index | no index | no index | no index | no index |
| 256 | no index | no index | **index** | no index | no index |

Migration 0026 installed **128**. At 128 all five shapes degrade to the same `Seq Scan` +
`Hash Join` + top-N heapsort. `scoped_bare` failing Bar 2 therefore costs nothing against the
statement it would replace: they fail it together.

It also retires the claim the premise rested on. "The unscoped candidate degrades gracefully,
keeping the HNSW index" (5b, 5f-8) holds at modulus 32 and at no other rung measured. It is a
property of that modulus, not of the shape, and 32 is not what shipped. The forced-generic plan
choice here is a cost comparison decided near its own margin, and it lands differently at
different moduli in both directions.

### 2. The generic plan is never reached unforced

`dense-plan-time.json`'s planning spike on the sixth call was read as Postgres *reaching* a
generic plan unforced. Forty unforced executions of each arm on one backend, the plan read every
time, at all three rungs: **the HNSW index is present in all forty, on every arm.**
`hnsw_lost_at_execution` is `null` everywhere. The spike is still there —
`local` at 128: `0.168, 0.261, 0.184, 0.207, 0.158, 8.595, 0.242, 0.169, 0.153, …` — and it is
Postgres *building* a candidate generic plan and pricing it, then going on planning custom. It
builds one; it does not use one.

So the forced arms are a bound on the damage, not a forecast of it.

## Under the plan Postgres actually uses, both candidates are a large win

At modulus 128, custom plan, as `zenith_app` with the policies in force:

| arm | partitions in plan | `Subplans Removed` | planning | execution | locks |
|---|---|---|---|---|---|
| `today` (shipped, unscoped) | 2 | `[127, 127]` | 8.417 ms | 2.143 ms | 1,161 |
| `local` (unscoped candidate) | 2 | none | **0.195 ms** | 0.869 ms | **18** |
| `shipped_scoped` | 2 | `[127, 127]` | 9.364 ms | 3.409 ms | 1,161 |
| `scoped_bare` | 2 | none | **0.165 ms** | 1.555 ms | **18** |

Both candidates fold the policy into a `One-Time Filter`, name the HNSW index, and carry no
`Append` at all — plan-time pruning, not executor-startup pruning. At 256 the same comparison is
23.1 ms → 0.134 ms and 2,313 locks → 18.

## The correctness bars, at all three rungs

- **Bar 4 — rows.** Against the literal scoped statement `search.dense()` sends: 50 shipped,
  50 candidate, `in_both` 50, `max_score_delta` **0.0**, 0 rows at a different rank. The same
  under one ordinary label.
- **Bar 5 — isolation.** 64 cells of the 8×8 grid, each asking a session that is tenant
  *context* for tenant *requested*'s rows **with tenant *requested*'s own documents as the
  scope**: 400 rows on the diagonal, **0 off it**, 0 foreign rows. Through the plan cache — nine
  calls as tenant 3, then tenant 5 on the same backend, custom and then forced generic — 50 rows
  and 0 foreign both times.
- **Bar 6 — the pair agrees.** The scoped function given all 39 of the tenant's documents returns
  exactly what the unscoped one returns: 50/50, `in_both` 50, `max_score_delta` 0.0, 0 at a
  different rank.
- **Bar 7 — locks.** 18, against a ceiling of 32, at every rung.

## Recommendation: ship both, with one condition written down

The hypothesis is false and the decision it was meant to unblock is still supported, for a
different reason than the hypothesis gave. At the modulus that shipped, the scoped candidate's
generic-plan degradation is identical to the degradation the shipped statement already has, and
the generic plan is not reached without being forced. Under the plan that is reached, both
candidates prune at plan time, hold 18 locks instead of 1,161, and return the same rows to the
bit.

**The condition, and it belongs in the migration's note rather than in a reviewer's memory:** at
modulus 256 the shipped scoped statement keeps the HNSW index under a forced generic plan and
`scoped_bare` does not. That is the one measured rung where the scoped candidate is worse than
today, and it is worse only under a forced generic plan. **If the partition modulus is ever
raised above 128, re-run `dense-two-functions` before assuming the scoped half is still free.**

If one decision is wanted rather than two, ship the unscoped function only: it is the half that
carries no such condition, and it is where the whole lock saving on the unscoped path lives.
"Ship neither" is not supported by these numbers — it would keep 1,161 locks and 8.4 ms of
planning per dense query to avoid a degradation the shipped statement already has.

### The exact SQL

`SECURITY INVOKER`, so the body runs under the caller's policies and the added qualifier is
redundant *with* the policy rather than a substitute *for* it. Neither function may be called
from an owner-side path: the `NULL` guard makes them return nothing when no tenant is set, which
is a fact about where they may be used and not a leak (`dense-plan-time-pruning.sql` 6b).

```sql
CREATE FUNCTION search.dense_candidates(
  q halfvec(1024), mdl text, ver text, want int)
RETURNS TABLE(chunk_id uuid, score double precision)
LANGUAGE plpgsql STABLE SECURITY INVOKER
AS $$
DECLARE v_tenant uuid := zenith_current_tenant();
BEGIN
  IF v_tenant IS NULL THEN RETURN; END IF;
  RETURN QUERY
  SELECT c.id, 1 - (e.embedding_half <=> q)
  FROM chunk_embeddings e
  JOIN chunks c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id
  WHERE e.tenant_id = v_tenant
    AND e.embedding_model = mdl AND e.embedding_version = ver
  ORDER BY e.embedding_half <=> q
  LIMIT want;
END;
$$;

CREATE FUNCTION search.dense_candidates_scoped(
  q halfvec(1024), mdl text, ver text, want int, docs uuid[])
RETURNS TABLE(chunk_id uuid, score double precision)
LANGUAGE plpgsql STABLE SECURITY INVOKER
AS $$
DECLARE v_tenant uuid := zenith_current_tenant();
BEGIN
  IF v_tenant IS NULL THEN RETURN; END IF;
  RETURN QUERY
  SELECT c.id, 1 - (e.embedding_half <=> q)
  FROM chunk_embeddings e
  JOIN chunks c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id
  WHERE e.tenant_id = v_tenant
    AND e.embedding_model = mdl AND e.embedding_version = ver
    AND c.document_id = ANY(docs)
  ORDER BY e.embedding_half <=> q
  LIMIT want;
END;
$$;
```

**`AND c.tenant_id = e.tenant_id` in the join is load-bearing and is not what `search.py` sends
today.** `search.dense()` joins on `c.id = e.chunk_id` alone. Without the tenant equality the
`chunks` side has no partition-key qualifier the planner can fold, so only the embeddings side
prunes at plan time and half the saving in the table above disappears. Both halves of the
measured arm carry it, and so does `as_shipped` in `dense-plan-time-pruning.sql`, which describes
it as "`search.dense()` with the schema changed and nothing else".

`docs` must never be NULL. A caller that passes NULL gets nothing back rather than everything,
which is the right direction, but it is the caller's job not to: `search.py` calls the scoped
function only where it would have written the scope clause.

### The two-functions-in-step risk

Real but small, and measurable rather than a matter of opinion. The two bodies differ by one
`AND` line; there is no shared clause built by concatenation and nothing that can drift silently
in the way a string-built `WHERE` can. Bar 6 is the check that would catch a drift — the scoped
function given every document the tenant owns must return exactly what the unscoped one returns,
and it does, at every rung. A migration shipping this pair should bring that check with it as an
integration test; the failure mode it guards is one function being edited and the other not, and
it costs one query to detect.

## What was not measured, and cannot be here

A tenant in this corpus is roughly 1,694 passages, so the `Seq Scan` the generic plan falls back
to reads 1,700 rows and costs 0.78 ms — cheaper than the shipped arm it replaces. Whether that
plan shape is still acceptable on a tenant of 100,000 passages is not answerable from this
corpus. It is also not a question this change introduces: at modulus 128 the shipped statement
reaches the same plan by the same route.

## Harness faults found by their own results

Recorded because each produced a wrong answer first:

- `float(x or 1)` reads a passing zero as a failure. Bars 4, 5 and 6 first reported failures with
  the passing numbers printed beside them.
- Bars evaluated over an empty rung list reported **seven passes** for a run that never reached
  the database. Fixed so an unmeasured rung fails every bar by name.
- The "another tenant's documents in scope" probe counted rows rather than *foreign* rows, which
  made an artefact of the synthetic corpus look like a leak: `assign` scatters one real document's
  passages across all eight tenants, so **40 of the 40 documents in the schema are held by more
  than one tenant** and a scope built from tenant 5's documents legitimately selects tenant 3's
  rows. It returned 50 rows and **0 foreign rows**. Now counted against `assign`, and the sharing
  is recorded per rung as `documents_shared_between_tenants` so it cannot be forgotten again.
  This also weakens Bar 5's grid by the same amount: off the diagonal the *qualifier* is foreign
  but the *scope* largely is not, so the grid tests the qualifier and the policy rather than the
  scope. A corpus where documents belonged to one tenant each would test more, and this one
  cannot be made into that without ceasing to be the real corpus.
