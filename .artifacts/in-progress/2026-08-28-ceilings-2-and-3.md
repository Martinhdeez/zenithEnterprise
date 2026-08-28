# Ceilings 2 and 3 — the vector index at a scale nobody has measured

**Date:** 2026-08-28 · **Status:** in progress

Two questions are open, and one piece of work answers both. That is the whole reason they
are planned together rather than separately.

## What is open

**Ceiling 3 — is binary quantisation usable?** `eval/quantisation.json` measured it at
13,549 vectors and found the gap to exact retrieval **widening with corpus size**:
`+0.0384` per decade at rescore 100, with the worst question falling 0.90 → 0.50 across less
than one decade. Extrapolated log-linearly to 300M passages the gap is 0.2137, i.e.
recall@10 ≈ 0.79, which is not shippable. But an extrapolation from four points inside one
decade is a hypothesis, not a finding. Binary is the difference between ~156,000 and
~980,000 documents on the same server, so the hypothesis is worth the cost of testing.

**Ceiling 2 — where does `hnsw.max_scan_tuples` belong?** `perf/iterative-scan-on-the-hot-path`
turned iterative scan on for every query, correctly. It could not calibrate the bound that
stops it: pgvector 0.8 defaults `max_scan_tuples` to 20,000, which is above this entire
13,549-vector graph, so no value could bind and — per ADR 0005 on unmeasured constants —
none was set. On a corpus where 20,000 is a small fraction, an unbounded-in-practice
iterative scan meets the global 10 s `statement_timeout` instead of a designed limit.

Both questions need the same thing: **a vector corpus one to two decades larger than the one
that exists.**

## The crux, and where this can go wrong

The generator decides whether any of it means anything.

Binary quantisation fails through **sign-pattern collisions**: two vectors whose 1024 signs
largely agree are indistinguishable in Hamming space regardless of their true distance. How
often that happens is a property of the *local density and cluster structure* of the
embedding space, not of the vector count. So a synthetic corpus that gets the geometry wrong
answers a different question than the one being asked, and does it convincingly.

Two obvious generators are both wrong, in opposite directions:

- **Random unit vectors** in 1024 dimensions are nearly orthogonal to each other. They have
  no cluster structure at all, Hamming distances separate cleanly, and binary would look
  far better than it is. This flatters.
- **Real vectors plus Gaussian noise** creates tight shells of near-duplicates around each
  original. Local density explodes, collisions explode with it, and binary looks worse than
  it is. This condemns.

The generator used here interpolates **between near neighbours**: pick a real vector, pick
one of its k nearest neighbours, take a point between them, renormalise. That densifies
regions the real corpus already occupies rather than inventing new ones or piling points on
top of existing ones — which is what actually happens when a real archive grows, since more
documents arrive on topics the archive already covers.

### The gate

The generator is not trusted because the argument above is plausible. Before any large
measurement is read:

1. Generate a synthetic set of **exactly 13,549** vectors from the real 13,549.
2. Measure binary's gap@10 on it, at every rescore width.
3. It must reproduce the real corpus's measured gap. **If it does not, the generator is
   wrong and every number above 13,549 is discarded.**

A generator that reproduces the known point is evidence; one that does not is a warning that
arrived in time. Both outcomes are worth the run, which is why this is a gate rather than a
formality.

## Stages

| # | Work | Answers |
|---|---|---|
| 1 | Generator + the 13,549 calibration gate | nothing yet — it licenses the rest |
| 2 | Gap curve at 50k, 150k, 500k, 1M | ceiling 3: is binary usable |
| 3 | `max_scan_tuples` where it can finally bind | ceiling 2: the missing constant |
| 4 | fp16 confirmed at scale | ceiling 3: does the safe option stay safe |

Stage 4 is not a formality either. fp16 was flat across 2k–13.5k, but "flat across one
decade" is the same evidence binary had before the trend appeared. The variant being adopted
gets tested at the same sizes as the one being doubted.

## Ladder and cost

Sizes are chosen to extend the existing curve by ~1.9 decades with four new points rather
than one distant point, because the **shape** decides this, not the endpoint. If the trend
holds log-linear through 1M, the extrapolation to 300M is credible and binary is finished.
If it flattens, the small-N slope was an artefact and binary is back.

Resources on this machine: 389 GB free disk, **7.75 GB RAM for the whole Docker VM** of which
TEI holds 3.4 GB, `shared_buffers` 128 MB. At 1M vectors the fp32 heap is ~4 GB and the fp16
index ~2.7 GB, so nothing is memory-resident and every measurement is disk-bound. That is a
limitation to state, not to hide: **latency figures from this run describe a machine under
memory pressure and are not the latency of the target server.** Recall figures are unaffected
by residency, and recall is what this measures.

No fp32 HNSW index is built above 13,549. Ground truth is an exact scan, which needs the
vectors and not an index, and an 8 GB fp32 index would cost build time for a baseline already
known to be 1.0000.

## The contingency, checked before it was needed

If binary fails, the alternative usually reached for is `pgvectorscale` — StreamingDiskANN
with statistical binary quantisation, built for exactly this and better than a hand-rolled
two-stage. **It is not available.** `pg_available_extensions` on the running ParadeDB image
(PostgreSQL 17.5) lists `vector 0.8.0` and `pg_search 0.15.26` and nothing else in this
family.

That makes it a change of base image, not a change of configuration, on a product that ships
on-premise and whose deployment story is `docker compose`. It stays on the table and it is
not a cheap fallback. The cheap fallback, if binary fails, is partitioning by tenant: it does
not shrink the index but it shrinks the *resident* set, which is the quantity that actually
has to fit.

## Results — ceiling 3

**The gate passed on magnitude and failed on sign, and the sign was the finding.** Worst
delta 0.0348 against a 0.05 tolerance, but all five deltas negative: the synthetic corpus
produces a *smaller* binary gap than the real one at a density that is higher. Density is not
the whole mechanism. It does not void the run — a corpus that flatters binary and still shows
it failing bounds the real degradation from below — but a magnitude test could not see the
difference between systematic bias and scatter, and that was a defect in this plan.

| corpus | d10 | ≈ real N | fp16 | binary r100 | binary r400 | worst question, r400 |
|---|---|---|---|---|---|---|
| real 13,549 | 0.2621 | — | 0.0000 | 0.0767 | 0.0140 | 0.60 |
| synthetic 3,750 | 0.2147 | ~89k | 0.0000 | 0.0419 | 0.0047 | 0.80 |
| synthetic 13,549 | 0.1689 | ~840k | 0.0000 | 0.0163 | 0.0163 | 0.80 |
| synthetic 50,000 | 0.1132 | ~35M | 0.0000 | 0.1140 | 0.0279 | 0.40 |
| synthetic 150,000 | 0.0605 | far beyond | **0.0047** | 0.1930 | 0.0767 | **0.10** |

**Binary is rejected**, and not on the mean. At rescore 400 the mean gap is still only 0.077
— 92% recall@10 — while the *worst single question* returns one of its ten true neighbours.
At rescore 100 and 200 it returns none. A search product is judged on its worst questions;
that is the argument F26 was built on and it applies here unchanged.

**fp16 is adopted and is no longer perfectly free.** 0.0047 at the densest point is the first
non-zero gap ever measured for it, against 0.0000 everywhere else. Negligible — 99.53%
recall@10 — but "flat forever" was the previous claim and it is now known to be false, which
matters more than the size of the number.

**What this leaves.** fp16 gives 3.00x. At 128 GB usable that is roughly 145,000 documents at
this corpus's 322.6 passages each, or ~586,000 at a corporate mix of 80. The million-document
target is not reached and binary was the route that would have reached it.

The remaining route is partitioning by tenant, and its value is different in kind: it does
not shrink the index, it shrinks the **resident** set. An installation with 200 tenants of
which ten are active holds 5% of its index in memory — the same order of magnitude binary
would have bought, at no cost in recall, paid for in operational complexity instead. Its one
blocking unknown is whether Postgres prunes partitions when the key comes from
`zenith_current_tenant()`, which is confirmed `STABLE` and therefore cannot prune at plan
time.

## Results — ceiling 2

**`hnsw.max_scan_tuples` stays at its default, and that is now measured rather than
assumed.** 150,000 vectors, halfvec index, five selectivities against five bounds,
`scan-bound.json`. All twenty-five arms return 50 of 50, and the plan column is what makes
that readable:

| filter admits | rows | plan chosen | candidates | effect of the bound |
|---|---|---|---|---|
| 50% | 75,000 | hnsw | 50 / 50 | none |
| 10% | 15,000 | hnsw | 50 / 50 | none |
| 2% | 3,000 | bitmap | 50 / 50 | none |
| 0.5% | 750 | bitmap | 50 / 50 | none |
| 0.1% | 150 | bitmap | 50 / 50 | none |

There are two regimes and the bound is irrelevant in both, for different reasons.

Above roughly 10% the planner uses HNSW, and iterative scan finds fifty admissible rows
long before it has walked twenty thousand tuples — at one row in ten it expects to walk
about five hundred. Below roughly 2% the planner **stops using the vector index at all**:
it reads the filter's index and sorts the survivors exactly, which is both correct and
fast (0.58 ms at 150 admitted rows). There is no iterative scan there to bound.

The crossover sits between those two, and the regime the bound was feared for — selective
enough to starve the walk, but not selective enough for the planner to leave — does not
exist on this shape of query. Writing a number down would be inventing a constant to
guard a case that has not been shown to occur, which is what ADR 0005 forbids.

**The limitation, stated rather than buried.** The filter here is one indexed inequality
on a `double precision`. The real policy is a conjunction — `tenant_id = …` on a btree
*and* `label_ids && …` on a GIN index — and a conjunction of two indexable predicates can
put the planner's crossover somewhere else. What this establishes is that the bound does
not bind on either side of a crossover; where exactly the crossover sits under the real
policy is a separate question, and `tenant-scale.json` already shows the planner choosing
HNSW at 61% and 53% label scopes, which is the same side of it.

## The partitioning blocker is not a blocker — measured

Everything above leaves partitioning by tenant as the only remaining route to the
million-document target, and partitioning had one unknown that could have killed it outright.
The partition key would be `tenant_id`, but the tenant does not arrive as a literal: it comes
from `zenith_current_tenant()`, which reads a GUC and is confirmed `STABLE`, not `IMMUTABLE`.
Plan-time pruning needs a constant. Without pruning, every query scans every partition and
the idea is worse than useless.

**Postgres prunes it at execution time.** Measured on the live database, eight list
partitions over the real 13,549 vectors, `backend/eval/partition-pruning.sql`:

```
->  Append (actual rows=5 loops=1)
      Subplans Removed: 7
      ->  Seq Scan on p2 chunks_1 (actual rows=5 loops=1)
            Filter: (tenant_id = (NULLIF(current_setting('zenith.tenant_id', true), ''))::uuid)
```

Seven of eight partitions removed, and in all three shapes that matter: the direct query, a
`PREPARE`/`EXECUTE` pair — which is what the driver actually sends and where a generic plan
could have defeated pruning — and the same query with the vector `ORDER BY` on top, which is
the shape that ships.

So the route is open. What partitioning buys is different in kind from what quantisation
buys: it does not make the index smaller, it makes the **resident** set smaller. An
installation holding 200 tenants of which ten are searching keeps a twentieth of its index
hot — the same order of magnitude binary quantisation would have bought, at no cost in
recall, paid for in operational complexity instead. That is a much easier price.

It is not free and this document does not pretend otherwise: N tenants means N HNSW indexes
to build and vacuum, a partition per tenant makes tenant creation a DDL operation, and
Postgres degrades on partition counts in the thousands. Those are the questions the next
piece of work has to answer. None of them is the one that could have ended it.

## Not in scope

Retrieval, fusion, ranking, the reranker, and every production code path. This creates a
scratch schema, measures, drops it. The fp16 *adoption* is a separate branch
(`perf/halfvec-index`, migration 0025) built on the same base.

## Branch discipline

Applying `CONTRIBUTING.md`'s parallel-work rules, which were written this week after six
consecutive merge conflicts in one file:

- **Base:** `integration/scaling` at `dbc653c`, for every branch in flight.
- **Alembic:** 0023, 0024 taken; **0025** is `perf/halfvec-index`; **0026** reserved.
  This branch adds no migration.
- **Ownership:** `perf/halfvec-index` owns `search.py`, the ingestion write path and
  `alembic/`. This branch owns `backend/eval/scale.py` and the `zenith_scale` schema. No
  file is claimed twice.
- **No registry edit.** `eval/__main__.py` discovers sweeps; this one declares `COMMAND`
  beside its own `run` and touches no shared file.
- **Database contention is a real conflict here, not just a merge one.** Two sessions
  building HNSW indexes in a 7.75 GB VM produce each other's noise —
  `vps-contention.json` measured a 6x latency distortion from exactly this. Heavy builds on
  this branch wait until `perf/halfvec-index` has taken its measurements.

## Hygiene

One schema, `zenith_scale`, dropped in a `finally` path, with the run printing its own
verification that neither the schema nor any new `SECURITY DEFINER` function survives —
`public` must hold exactly 7. This follows the two orphaned `SECURITY DEFINER` functions
found and removed from this database earlier today, left behind by the F18 investigation and
declared in no migration, branch or file.
