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
