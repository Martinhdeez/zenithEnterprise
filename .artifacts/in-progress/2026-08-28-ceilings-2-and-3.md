# Ceilings 2 and 3 — the vector index at a scale nobody has measured

**Date:** 2026-08-28 · **Status:** in progress

Two questions are open, and one piece of work answers both. That is the whole reason they
are planned together rather than separately.

## What is open

**Ceiling 3 — is binary quantisation usable?** `eval/quantisation.json` measured it at
13,549 vectors and found the gap to exact retrieval **widening with corpus size**:
`+0.0606` per decade at rescore 100, with the worst question falling 0.90 → 0.40 across less
than one decade. Extrapolated log-linearly to 300M passages the gap is 0.3300, i.e.
recall@10 ≈ 0.67, which is not shippable. But an extrapolation from four points inside one
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

Stage 4 is not a formality, and it became less of one. fp16 was reported flat across
2k–13.5k; that reading came from a `quantisation.json` in which fp16 was measured by
sequential scan, and the corrected run has it widening at **+0.0386 per decade** — within
noise of uncompressed fp32's +0.0445, because what widens is the HNSW graph at
`ef_search = 100` and not the representation. So the variant being adopted gets tested at the
same sizes as the one being doubted, and the question at those sizes is no longer "is fp16
flat" but "does binary degrade faster than the graph already does".

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
vectors and not an index, and an 8 GB fp32 index would cost build time for a baseline this
sweep does not use: what is measured here is quantisation error alone, against exact fp32,
so the fp32 arm is the ground truth rather than a variant. The earlier version of this
paragraph justified the omission differently — "a baseline already known to be 1.0000" — and
that justification was void, because `quantisation.json`'s fp32 index-recall row was a
sequential scan compared against itself. The corrected run puts fp32 through its own HNSW
index at **0.9600** at 13,549 and widening. The omission still stands on the reason above;
it did not stand on the one it was given.

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

**Binary is rejected at rescore 100 and survives at rescore 400 — and the first version of
this section said it was rejected outright.** That was measured over all 43 questions,
twelve of which the corpus cannot answer. An unanswerable question's exact top ten is an
arbitrary set, so a compressed index was being failed for not reproducing noise, and the
noise dominated the worst-case figure. Over the 31 answerable questions:

| corpus | d10 | fp16 | binary r100 | binary r400 | worst r100 | worst r400 |
|---|---|---|---|---|---|---|
| real 13,549 | 0.2621 | 0.0000 | 0.0484 | 0.0032 | 0.5 | 0.9 |
| synthetic 3,750 | 0.2147 | 0.0000 | 0.0323 | 0.0000 | 0.7 | 1.0 |
| synthetic 13,549 | 0.1689 | 0.0000 | 0.0419 | 0.0129 | 0.7 | 0.8 |
| synthetic 50,000 | 0.1132 | 0.0000 | 0.0645 | 0.0097 | 0.4 | 0.8 |
| synthetic 150,000 | 0.0605 | 0.0097 | 0.1419 | 0.0258 | 0.3 | 0.5 |

The worst question at rescore 400 goes from 0.10 to **0.50** at the densest point and 0.80
at the one standing in for ~35M passages. Rescore 100 is still finished — 0.1419 mean, 0.3
worst — but the width is a query-time choice costing four hundred exact distance
computations, and migration 0025 keeps the fp32 vectors that rescoring needs.

The corroboration is worth as much as the numbers, and it is worth less than this paragraph
first claimed. It read: `quantisation.json` measures through an HNSW index over 30 answerable
questions and puts the real corpus's r100 gap at 0.0467, this file measures by exact scan
over 31 and gets **0.0484**, two harnesses and two methods agreeing. They were not two
methods. `quantisation.py` was leaving `enable_indexscan = off` on for its arms, so its
binary shortlist was an exact sequential Hamming scan — the same computation this file does,
which is why the two agreed to within 0.002.

Corrected, `quantisation.json` puts the r100 gap at **0.0667** through a real HNSW walk at
`ef_search = 100`, against this file's **0.0484** by exact scan. The two now differ by the
graph, in the expected direction and by about the amount the graph costs elsewhere in that
report (fp32 itself loses 0.0400 at the same size), and each is the right number for its own
question: this file isolates what binary quantisation destroys, `quantisation.json` reports
what deploying it would actually retrieve. The trend is what both agree on, and that
agreement is real.

**The gate is properly calibrated now.** Worst delta 0.0226 and, more importantly, the
systematic bias is gone: `mixed` rather than `flatters_binary`. The noise questions were
producing the bias as well as the inflated worst case.

**fp16 is adopted and is not perfectly free.** 0.0097 at the densest point against 0.0000
everywhere else — about one percent of recall@10, in a density regime no planned corpus
reaches. Negligible, and worth recording because "flat forever" was the previous claim. That
is quantisation error alone, by exact scan. Through an index it is joined by the graph's own
loss, which the corrected `quantisation.json` puts at 0.0400 at 13,549 for fp16 *and* for
uncompressed fp32 alike; the adoption rests on fp16 matching fp32, not on either being exact.

**What this leaves.** fp16 gives 3.00x with no practical cost. Binary at rescore 400 gives
18.84x and is defensible into the tens of millions of passages, degrading past that.
Neither is a decision this document takes: binary now needs a product change — a two-stage
retrieval path — which is a separate piece of work with its own risks, and fp16 is already
shipped.

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
