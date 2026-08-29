# Choosing the HASH modulus

**Status:** measured
**Evidence:** `backend/eval/modulus-cost.json`, `backend/eval/modulus_cost.py`
**Reopens:** the reasoning behind MODULUS 256 in `backend/eval/partition-shape.json`

ADR 0009 partitions `chunks` and `chunk_embeddings` by `tenant_id`, HASH.
`partition-shape.json` recommended **256**, and it chose that number against a ceiling: the
shared lock table runs out during *planning* and the query never runs, so 256 was "as many as
we can safely afford". `lock-budget.json` then sized `max_locks_per_transaction`, verified 32
concurrent queries at 256 partitions, and **took that ceiling away**.

This document is what the modulus costs once nothing is stopping it. Every figure comes from
a run written to disk under `backend/eval/`, and none of it was typed from memory.

---

## The one paragraph

**The benefit of a larger modulus is bounded and runs out early; the cost is not bounded and
grows faster than the modulus does.** A tenant's query reaches `s + (1 - s) / P` of the corpus,
where `s` is its own share and `P` the modulus — so the whole benefit available from *any*
modulus is the `(1 - s) / P` term, which halves per doubling. Planning, measured across five
doublings, roughly **triples** per doubling. Under the capacity plan's own Zipf-1.0 assumption
at 200 tenants the largest tenant reaches 0.1966 of the corpus at MODULUS 32 and 0.1710 at
1024 — **it owns 0.1701 and can never do better** — while a request's planning goes from
2.88 ms to past the point where this machine can plan it at all.

256 is not wrong. It is past the knee.

---

## What was measured

Seven rungs — unpartitioned, 32, 64, 128, 256, 512, 1024 — against two synthetic tenant
populations over the installation's real 13,549 passages, with the production index set on
every partition and a row-level security policy on each. Per rung: planning and execution for
each of a search's statements, the partitions each plan actually names, the locks a request
holds, and the rows a tenant's query reaches against the rows it owns.

The server at the time of the run: PostgreSQL 17.5, `max_locks_per_transaction` **64**,
`max_connections` 100, `shared_buffers` 128 MB, `plan_cache_mode` `auto`,
`enable_partitionwise_join` off. `chunks` and `chunk_embeddings` were **unpartitioned** on the
installation itself — migration 0026 was rolled back by a neighbouring branch while this ran,
which is why the end-to-end anchor below is an unpartitioned one.

All four bars declared in `modulus_cost.py` before the run pass at every reachable rung: the
planning curve moved by 39–45× against a bar of 4×; `Subplans Removed` is `P - 1` on both
`Append` nodes of the dense join at every partitioned rung; the two routes to widening agree
everywhere; and the two lexical forms return the same 50 chunk ids at the same ranks with no
NULL score everywhere.

---

## Curve 1 — planning, and it is superlinear

Median of the custom plans, per statement, at 200 tenants. `bm25 pruned` is the lexical half
with the plpgsql fix `unpruned-plpgsql-pruning.sql` measured; `bm25 shipped` is
`zenith_lexical_search` as migration 0022 writes it; `tsvector` is the engine this
installation is configured with.

| MODULUS | dense join | hydrate | bm25 pruned | tsvector | bm25 shipped |
|---|---|---|---|---|---|
| unpartitioned | 0.116 | 0.042 | 0.307 | 0.066 | 0.119 |
| 32 | 1.702 | 0.953 | **0.223** | 2.372 | 2.501 |
| 64 | 3.531 | 1.957 | **0.347** | 7.206 | 5.870 |
| 128 | 12.266 | 4.641 | **0.257** | 30.950 | 16.762 |
| 256 | 28.103 | 9.011 | **0.278** | 105.973 | 48.629 |
| 512 | 103.669 | 26.409 | **0.273** | 421.672 | 170.863 |

And the same thing as a request — the statements a single search plans, added up:

| MODULUS | after stage 02 (bm25, pruned) | as the system stands (bm25, unpruned) | `tsvector` engine, as installed today |
|---|---|---|---|
| unpartitioned | 0.47 | 0.28 | 0.22 |
| 32 | 2.88 | 5.16 | 5.03 |
| 64 | 5.83 | 11.36 | 12.69 |
| 128 | 17.16 | 33.67 | 47.86 |
| 256 | 37.39 | 85.74 | 143.09 |
| 512 | 130.35 | 300.94 | 551.75 |

Three things to read off it.

**It is not linear.** Everyone downstream of `partition-shape.json` has been extrapolating a
straight line. The dense join goes 1.70 → 3.53 → 12.27 → 28.10 → 103.67 ms over four
doublings — a factor of **2.1 to 3.7 per doubling**, not 2. A request's planning after the
stage-02 lexical fix goes 2.88 → 5.83 → 17.16 → 37.39 → **130.35 ms**: 45× for a 16× modulus.
The per-execution readings within a rung are stable to a few per cent, so this is the curve
and not the machine.

**The plpgsql fix removes the modulus from the lexical half entirely.** `bm25 pruned` is flat
— 0.223 ms at 32 and 0.273 ms at 512 — because its plan names **one** partition at every rung
rather than `P`. Its lock footprint is flat too: **12 locks at every modulus, against 3,078
for the shipped form at 512.** Nothing else in the request does this, and that is the point:
the dense join, `hydrate` and the `tsvector` lexical engine prune at *executor startup*, so
the planner has already built and priced every partition before anything is discarded.

**The plan cache pays the unpruned cost exactly once and then stops.** The pruned arm's
per-execution series has one spike, always on the sixth execution — 2.79 ms at 32, 16.10 at
128, 155.85 at 512 — and returns to 0.2 ms after it. That is Postgres building a candidate
generic plan, which has no parameter to prune on, pricing it, rejecting it, and keeping the
custom one. `plan_cache_mode = auto` is safe for this form and the spike is its whole price.

The `bm25 shipped` arm does the opposite: it adopts the generic plan and stops planning
altogether from the seventh execution. It is cheaper per query and scans every partition for
ever. `lock-budget.json` measured the product's own driver path and found **11 custom plans
and 0 generic** for the dense query, so the dense half pays its planning on every execution.

---

## Curve 2 — widening, and it runs out

How much a tenant's query reaches against how much it owns. Measured three ways that have to
agree — rows counted in the partition the tenant hashed into, rows predicted by reproducing
Postgres's partition hash in arithmetic, and `satisfies_hash_partition` — and they do, at
every rung.

At 200 tenants under Zipf 1.0, largest tenant 2,305 passages (0.1701 of the corpus), median
tenant 23:

| MODULUS | tenants sharing the partition | largest: widening | largest: share of corpus reached | median tenant: widening |
|---|---|---|---|---|
| unpartitioned | — | 5.878 | 1.0000 | 589.087 |
| 32 | 4 | 1.050 | 0.1786 | 7.217 |
| 64 | 3 | 1.040 | 0.1770 | 2.130 |
| 128 | 1 | 1.000 | 0.1701 | 1.000 |
| 256 | 1 | 1.000 | 0.1701 | 1.000 |
| 512 | 1 | 1.000 | 0.1701 | 1.000 |

One built population is one sample of the hash's luck, so the same question is asked again
over forty independent draws per cell, for tenant counts from 5 to 1,000 and four size
distributions. That model is arithmetic, and it is only allowed to be arithmetic because the
arithmetic was shown to reproduce the schema exactly at every built rung.

**Largest tenant's share of the corpus reached, Zipf 1.0, mean of forty draws:**

| tenants | 32 | 64 | 128 | 256 | 512 | 1024 | owns |
|---|---|---|---|---|---|---|---|
| 20 | 0.2947 | 0.2831 | 0.2817 | 0.2799 | 0.2784 | 0.2780 | **0.2780** |
| 50 | 0.2515 | 0.2330 | 0.2279 | 0.2285 | 0.2226 | 0.2227 | **0.2223** |
| 200 | 0.1966 | 0.1803 | 0.1745 | 0.1746 | 0.1722 | 0.1710 | **0.1701** |
| 1000 | 0.1650 | 0.1478 | 0.1399 | 0.1376 | 0.1363 | 0.1344 | **0.1336** |

The last column is the floor, and the modulus never reaches below it.

---

## The rule

```
share_of_corpus_reached  =  s + (1 - s) / P
widening                 =  1 + (1 - s) / (P · s)
```

`s` is the tenant's share of the corpus. `P` is the modulus. **There is no tenant-count term:
T cancels.** A tenant always reaches its own rows; every other tenant lands in its partition
with probability `1 / P` regardless of size, so what the rest of the corpus contributes is its
whole weight over the modulus.

The formula is evaluated beside the draws in every cell of the model, as
`closed_form_over_measured`. Over the 144 Zipf cells it sits between **0.948 and 1.041** — the
drift is at the low moduli, where forty draws is a small sample of whether the second-largest
tenant happened to collide.

Rearranged, it is the thing an operator can act on. **To hold a tenant of share `s` to at most
`ε` extra rows:**

```
P  ≥  (1 - s) / (ε · s)
```

| the tenant you care about | ε = 10% | ε = 5% |
|---|---|---|
| holds half the corpus (s = 0.5) | 10 → **16** | 20 → **32** |
| holds a fifth (s = 0.2) | 40 → **64** | 80 → **128** |
| holds a twentieth (s = 0.05) | 190 → **256** | 380 → **512** |
| holds a thousandth (s = 0.001) | 9,990 | 19,980 |

Read the last row as the warning it is. **For a small tenant the rule has no useful answer,
and that is not a failure of the rule.** A tenant holding a thousandth of the corpus cannot be
given a partition of its own by hashing; what it gets instead is an absolute cost of
`(1 - s) / P` of the corpus, which at P = 128 is 0.8% — small in the only terms that turn into
milliseconds. Widening as a *ratio* is frightening for small tenants and nearly free; widening
as a *share* is what a query pays for.

### In prose, for an operator with no access to this repository

> Partitioning by tenant stops a customer's search from reading the other customers' data.
> How much it stops depends on one number: the modulus, which is how many pieces the table is
> cut into.
>
> A customer's search always reads that customer's own documents. On top of that it reads
> about **one modulus-th of everybody else's**. So at 64 partitions a customer reads its own
> corpus plus 1.6% of the rest; at 256, its own plus 0.4%.
>
> That is the entire benefit, and it gets four times smaller every time you quadruple the
> modulus. Meanwhile **the database's planning time roughly triples for every doubling of the
> modulus**, because the planner considers every partition before discarding all but one.
>
> So: **choose the smallest modulus at which your largest customer's search reads mostly its
> own data.** Take that customer's share of your whole corpus — if it holds a fifth of your
> documents, that is 0.2 — and read the modulus off the table above. For almost every
> installation the answer is between 32 and 128. Going higher buys a fraction of a per cent
> and costs tens of milliseconds on every search.
>
> If you have very few customers, use a small modulus. Two customers do not need 256
> partitions; they need enough not to be in the same one, and 32 is already generous.

---

## What the largest tenant gets, specifically

Nothing, past about 64.

`partition-shape.json` records this installation's one real datum about size: the larger of
two tenants holds **0.6106** of the corpus, against the 0.5 a uniform split would give it. Put
that into the rule and the largest tenant reaches `0.6106 + 0.3894 / P` — 2.0% extra at
MODULUS 32, 0.25% at 256. The difference between those two moduli, for the tenant whose
latency is quoted back at you, is under two per cent of the rows it reads. The difference in
planning is 2.88 ms against 37.39 ms.

Under the plan's Zipf-1.0 at 200 tenants the largest tenant would hold 54.8M of 322M passages,
and it reaches 1.156× that at MODULUS 32 and 1.005× at 1024. **Uniform hashing cannot help a
tenant that is large on its own**, and no modulus changes the 54.8M.

The tenant the modulus *does* help is a small one that collides with a big one. Under Zipf 1.0
at 200 tenants the 99th-percentile tenant reaches 0.1982 of the corpus at MODULUS 32 and
0.0851 at 1024 — and 0.0851 is roughly the second-largest tenant's own share, which is where
it stops, because that is the neighbour it cannot be separated from often enough to matter.
Most of that fall happens between 128 (0.1714) and 256 (0.1050); the rest of the ladder buys
0.02 of the corpus for four times the planning.

---

## Where the total is, and why the measured minimum is not the recommendation

The measured total — planning plus execution, per request — is smallest at the **bottom** of
the ladder: 9.48 ms at MODULUS 32 against 138.24 ms at 512, with the unpartitioned pair at
9.50 ms. Only two rungs sit inside the 10% flat region declared before the run, and the
harness therefore refuses to name a single optimum for that population.

**That measured minimum is a statement about this corpus and not a recommendation.** 13,549
passages over 200 tenants is 68 rows a tenant; the widening a small modulus causes costs
nothing in milliseconds at that size, and the dense stage's execution barely moves across the
whole ladder (6.02 to 7.65 ms). The benefit of partitioning at all is invisible here and is
not in doubt: ADR 0009's target is 322.6M passages, where the unpartitioned rung's
`widening = 5.878` for the largest tenant is the difference between reading 54.8M rows and
reading 322M.

So the two curves are reported separately and only one of them is converted to milliseconds.
Planning transfers, because it is a property of the plan and the catalogue. Row placement
transfers, because it is a property of the hash. Execution milliseconds over 13,549 passages
do not transfer and are not used to choose anything.

---

## Locks, which are no longer binding but are still linear

| MODULUS | locks per request | lexical half alone, as shipped | lexical half alone, pruned |
|---|---|---|---|
| unpartitioned | 9 | 6 | 6 |
| 32 | 297 | 198 | 12 |
| 64 | 585 | 390 | 12 |
| 128 | 1,161 | 774 | 12 |
| 256 | 2,313 | 1,542 | 12 |
| 512 | 4,617 | 3,078 | 12 |

Exactly `9P + 9`, confirming `lock-budget.json`'s nine relations per partition-pair at every
rung. Two things worth noting beside it.

The lexical half's locks are **not additive** with the rest of a request: the dense join
already opens every partition, so within one `tenant_session` the pruned lexical form saves
planning and saves nothing on locks. It saves 1,530 locks only where it is the whole
transaction — an ingestion, a diagnostic, a background job.

`max_locks_per_transaction` was **64** for this run, nominally 6,400 slots, which one request
at MODULUS 512 fills to 72%. That setting belongs to `chore/partition-lock-budget` and was not
touched here; it is recorded per rung because it moved *during* this work, from 1024 to 64,
when a neighbouring branch's container was recreated.

---

## The rung that could not be measured

**MODULUS 1024 is not reachable on this installation, and the failure is not the lock table.**

Four attempts, four failures, all on the same statement. The Linux OOM killer terminates the
backend planning

```
EXPLAIN (ANALYZE, ...) SELECT c.id, ts_rank_cd(c.tsv, q) AS score
FROM zenith_modulus.chk c, to_tsquery(...) q WHERE c.tsv @@ q ORDER BY score DESC, c.id LIMIT $2
```

and PostgreSQL then terminates every other backend and goes into crash recovery — which the
report identifies for itself, in `grid.8.1024.aftermath`, from the connection refusal that
only a recovering cluster produces. One attempt reached the statement timeout instead:
**planning that one statement at MODULUS 1024 exceeds 300 seconds.**

It happened once with a backend that had climbed the whole ladder and three times with a
freshly recycled one, so it is not accumulated relation cache. It is the `tsvector` lexical
statement every time: six indexes on `chunks` × 1,024 partitions is 6,144 index paths to
price before one partition is chosen.

The host is a 7.75 GB VM shared with the rest of the stack, so the exact threshold is a
property of this machine and not of PostgreSQL. What transfers is the shape: **planning memory
grows with the modulus at least as fast as planning time does**, and a modulus large enough
takes the cluster down rather than merely slowing it. Nothing on the reachable part of the
ladder suggests 1024 would be worth reaching if it were reachable.

`max_locks_per_transaction` was not raised to chase it. At 1024 a request would hold 9,225
locks against 6,400 nominal slots, which `partition-shape.json` observed a single query
exceeding and surviving — but that surplus is unreserved shared memory belonging to every
other backend, and the failure here happened before the lock table was ever the constraint.

---

## Caveats

- **Milliseconds of execution on this machine are indicative; planning, `Subplans Removed`,
  the partitions a plan names, lock counts and row placement are the sound half.**
  `partition-shape.json`'s caveat, unchanged, and the reason widening is carried as a ratio.
- **The size distribution is an assumption.** Zipf over the tenant rank, exponent stated per
  row. No customer size distribution exists in this repository. What is *not* an assumption is
  that skew exists: 0.6106 against 0.5, measured.
- **The two populations are the replicate.** Planning depends on the modulus and not on what
  the partitions hold, and that is checked rather than assumed: 8 tenants and 200 tenants give
  2.78/2.88 ms at MODULUS 32 and 44.67/37.39 ms at 256 for the same request. The two agree to
  within about 20%, which is this machine's spread, while the curve itself moves by 45×.
- **Two other measurement branches shared the cluster.** One earlier pass was killed by a
  neighbour's lock exhaustion and is not in the report; every rung records the cluster's lock
  pressure at the moment it was measured so that a rung a neighbour stepped on can be told
  from a rung that is genuinely out of reach.
- **`enable_partitionwise_join` is off**, which is the installation's setting. With it on, the
  dense join would plan per-partition joins and both its planning cost and its pruning would
  change. That is a separate measurement and this one makes no claim about it.
- **The end-to-end anchor is unpartitioned.** 826.21 ms median over 12 questions, of which the
  reranker is 748.87 ms — **90.6%**. Whatever the modulus does, it does against a total that is
  nine parts cross-encoder. At MODULUS 256 the request's planning after the stage-02 fix is
  37.39 ms, 4.5% of that total; at 512 it is 130.35 ms, 15.8%; on the `tsvector` engine the
  installation runs today it is 143.09 ms and 551.75 ms, 17% and 67%.
