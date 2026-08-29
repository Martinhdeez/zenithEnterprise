# ADR 0009 — `chunks` and `chunk_embeddings` are partitioned by tenant

**Status:** Accepted; stage 0 landed, stages 1–3 planned in
`.artifacts/specs/2026-08-29-ceiling-3-architecture.md`

## Context

Every measurement in this record was taken against the same corpus — **42 documents, 13,549
passages, 322.6 passages per document** (`quantisation.json` `corpus`) — and every figure at
a larger size is a projection from it. The target is a million documents on one on-premise
machine, which at this corpus's density is **322.6M passages**.

At that size one HNSW index over `chunk_embeddings` is **820.2 GiB** of resident graph
(`quantisation.json` `projection.this_corpus.index_gib_by_documents.fp16`, at the measured
2,729.9 bytes per vector). That is the ceiling. Four levers were measured against it, and
three of them shrink the index:

| lever | factor | verdict | evidence |
|---|---|---|---|
| fp16 | 3.00× | **shipped**, migration 0025 | `quantisation.json` `smaller_than_fp32`; headline Recall@8 0.90 and Recall@1 0.6667 on both arms |
| `svd_512` | 2.00× | measured, not built | `dimensions.json`: index recall@10 0.9467 against 0.9667, worst question unchanged at 0.50 |
| coarse-to-fine (`chunks16/mean`) | 15.65× | measured, **does not scale alone** | `coarse-dims.json` `memory.factor`; `coarse-scale.json` `shape` |
| IVF instead of HNSW | 17,954× resident | measured, **no** as a global replacement | `index-shape.json` `projection.fp16.sizes.322000000` |

All four hit the same wall. With 322.6M passages in **one** index, either the index is
resident or every query reads a fraction of it — and a fraction of 322.6M is millions of
vectors whatever the bytes per vector are. IVF is the clearest demonstration, because it is
the lever that does not shrink the index at all but replaces it: its resident set is
centroids only, 49,004,544 bytes (46.7 MiB) against 818.7 GiB of HNSW graph, and it reaches
HNSW's recall by reading **10% of the corpus — 81.9 GiB, roughly 44 seconds per query**
(`index-shape.json`; the seconds are a projection, see *Evidence*). The memory comes back as
page cache with interest.

Coarse-to-fine looks like the exception and is not. Its `open` is a fixed fraction wearing a
count's clothing: fitted across five synthetic rungs from 13,549 to 1,354,900 passages,
**`open ~ N^1.112`** at the operating point that reproduces the page today, and the fraction
of passages the fine stage actually reads sits at **3.11%, 3.19%, 3.76%, 5.09%, 6.50%** across
those rungs with no downward trend (`coarse-scale.json` `shape.0.9355` and
`synthetic.rungs[*].required_open_exact`). The mechanism is visible in the same file: the mean
number of distinct groups holding a question's true top ten climbs from **5.9 to 9.94** as the
corpus grows, so a fixed `open` loses more of them at every size.

**Partitioning by tenant is the only lever that changes N**, and N is what every number above
depends on.

## Decision

**`chunks` and `chunk_embeddings` are partitioned by `tenant_id`.** A query walks one
tenant's graph, not the installation's.

The consequence for capacity is arithmetic rather than measurement. At the plan's sizing —
200 tenants sharing 322.6M passages, ten searching at once, on fp16 at `svd_512`'s measured
1,365.8 bytes per vector (`dimensions.json` `bytes_per_vector.svd_512`) — a tenant holds 1.6M
passages and 2.2 GB of index, and ten active tenants are 22 GB resident rather than 820.2
GiB. The uniform 200-tenant split is the plan's assumption and not a measured distribution;
see *What is open*.

### The second argument, which is not about memory

With the planner defect corrected (below), `quantisation.json` `trend` says something the
capacity argument does not: **fp32 — uncompressed, unprojected, ungrouped — loses 0.0400 of
recall@10 to HNSW at `ef_search = 100`, and the gap widens with corpus size at +0.0445 per
decade.** fp16 is indistinguishable: 0.0400 at the same size, +0.0386 per decade.
`index-shape.json` corroborates independently, putting HNSW fp32 at recall@10 0.9667 against
exact at the same `ef_search` over 30 questions.

**The graph has its own scaling wall and it arrives before the storage one.** Compression does
not touch it — fp16 and fp32 degrade at the same rate. A bigger machine does not touch it.
Only a smaller N does.

Extrapolated log-linearly from that fit, the projected gap at 322.6M is 0.2348 and at 1.6M is
0.1322. A query against a 1.6M-passage partition instead of a 322.6M-passage index moves 2.3
decades back down the curve, so **partitioning is expected to make recall better, not merely
cheaper.**

### Being exact about that gain, because it is easy to overstate

Three numbers with three different baselines, and conflating them would be the obvious way to
misread this ADR:

- **0.0400** is measured, at 13,549 passages, against exact fp32 retrieval.
- **+0.10** is the *difference between two projections* — the fitted gap at 322.6M minus the
  fitted gap at 1.6M. It is not measured anywhere and it is not the 0.0400.
- **0.1322** is the projected residual: what a 1.6M partition would still be losing to exact
  retrieval after partitioning. It is three times the gap measured today.

Partitioning removes the *growth* of the gap, not the gap. What can be recovered is bounded
above by the gap at 322.6M, and what remains afterwards is the gap at the partitioned N — and
neither has been measured at either size. Anyone who quotes "partitioning buys ten points of
recall" without the word *projected* beside it has quoted this ADR wrongly.

### The third argument: RLS and a shared graph do not compose

`tenant-scale.json` measured what a selective RLS predicate costs an HNSW walk over a shared
graph, at 13,549 vectors, `ef_search = 100`, `candidates = 50`:

| scope | share of graph | rows returned (mean of 50) | questions returning nothing | dense recall |
|---|---|---|---|---|
| M0 baseline, every label | 61.06% | 47.42 | 0 | 0.9042 |
| Cyber Standards Archive | 38.94% | 10.23 | 19 of 43 | 0.1995 |
| label `legal/contracts` | 14.61% | 7.77 | 23 of 43 | 0.1553 |

The plan sample for the 38.94% tenant is the mechanism in one line: `hnsw_rows: 8`,
`rows_removed: 92`. HNSW walks the shared graph and returns neighbours; the policy then
discards the ones belonging to somebody else, and what survives is whatever is left.

This is why `hnsw.iterative_scan = relaxed_order` is set unconditionally on every dense query
today (`tenant-scale.json` `shipped_sets_iterative_scan`). It repairs recall — 0.1995 → 0.8642
and 0.1553 → 0.9251 — and it is a rescan: median dense latency goes 0.78 → 2.24 ms at 38.94%
and 0.90 → 4.72 ms at 14.61%. **The cost grows as the accessible fraction shrinks**, and at 200
tenants a tenant's share of a shared graph is around 0.5%, an order of magnitude below the
smallest fraction measured here. A partition makes the predicate trivially true inside the
index instead of a filter applied after it — structurally the same correction migration 0022
made for BM25 in ADR 0002, one layer down.

## Consequences

### The one that is not a benefit: partitions do not inherit the parent's RLS policy

Reproduced, not feared, in `backend/eval/partition-rls.sql`. A partitioned parent with
`ENABLE ROW LEVEL SECURITY` and this project's exact policy template:

```
relname | rls_enabled | own_policies
chunks  | t           | 1
p2      | f           | 0
```

With `zenith.tenant_id` set to tenant 1, a query through the parent correctly returns tenant
1's 10,000 rows. A query against partition `p2` returns **tenant 3's 10,000 rows in full**, to
`zenith_app`. A partition is an ordinary table with its own grants and its own — absent —
policies.

The exposure needs a grant on the partition and nothing grants one today. But
`GRANT ... ON ALL TABLES IN SCHEMA public TO zenith_app` would, and so would a migration
written by anyone who assumed inheritance. **It is invariant 1 broken by a schema change**, and
it fails in the direction this repository refuses everywhere else: the ordinary path keeps
working, so nothing surfaces. There is no symptom, only a leak.

The mitigation is stage 0 of the plan and is a hard prerequisite — no table is partitioned
before it lands. **It has landed**, and it is three things:

1. `app.core.partitions.create_partition` emits the `CREATE TABLE`, the
   `ENABLE ROW LEVEL SECURITY` and the `CREATE POLICY` as one operation. DDL is transactional
   in Postgres, so there is no state in which the partition exists and its policy does not.
   It is deliberately an Alembic helper and not a SQL function: a SQL function would have to
   be created by a migration and would then live in the schema as a string nobody greps —
   the property that made `SECURITY DEFINER` the unauditable third class of bypass under
   invariant 2 — and a `SECURITY DEFINER` one would widen that surface to save three lines.
   The author who assumes inheritance is writing Python, so the thing that stops them is a
   Python identifier.
2. `tests/integration/test_partition_rls_guard.py` enumerates every partition of every
   RLS-protected table and fails unless each carries row-level security and a policy of its
   own — the shape of `tests/integration/test_security_definer_audit.py`, a list somebody has
   to justify rather than a convention somebody has to remember. Nothing is partitioned yet,
   so those assertions currently pass vacuously; **a guard that has never failed is not a
   guard**, so the test also builds a bare partition in a schema of its own, proves the check
   reports it and proves the rows really do reach the wrong tenant, then proves the helper
   closes both, and rolls back.
3. `partition-rls.sql` kept under `backend/eval/` as the reproducible demonstration.

Neither piece enforces its own use: `CREATE TABLE ... PARTITION OF` by hand still compiles,
and no helper can prevent that. Point 2 is what catches it, which is why the guard is the
prerequisite and the helper is only the convenience.

### What is verified

**Runtime pruning works, including under the shape that ships.** This was the unknown that
could have ended the idea outright: the partition key does not arrive as a literal, it
arrives from `zenith_current_tenant()`, which reads a GUC and is `STABLE` rather than
`IMMUTABLE`, and plan-time pruning needs a constant.

`backend/eval/partition-pruning.sql` — eight list partitions over the real 13,549 vectors, on
the live database — gives `Subplans Removed: 7` of 8 in three shapes: the direct query, a
`PREPARE`/`EXECUTE` pair (where a generic plan could have defeated it), and the same query
with the vector `ORDER BY` on top. It is honest about its scope: owner role, a bare equality,
no policy.

`backend/eval/partition-rls-policy-pruning.sql` closes the gap that leaves, and it is the one
to read, because four things separate the narrow probe from what will actually run and each
could plausibly have defeated pruning. The predicate is a **conjunction**, not an equality —
a planner that could not see past `label_ids = '{}' OR label_ids && zenith_current_labels()`
to the tenant clause would prune nothing. It arrives **injected by a policy** rather than
written by the query author, and the probe's `SELECT` carries no `WHERE` at all, so the
predicate is entirely the policy's. It runs as **`zenith_app`**, the role the policy applies
to — as the owner the policy is never consulted, so the earlier probe never exercised the
path. And **every partition carries its own copy of the policy**, which is only possible
since stage 0 and is the arrangement that will be deployed. Same result: `Subplans Removed: 7`
of 8, appearing twice — once for the InitPlan and once for the scan the `ORDER BY` drives —
with the full conjunction visible in the filter of the one partition that survives.

It loads real vectors from `chunk_embeddings` rather than random ones, because the `ORDER BY`
has to produce a plan the planner would actually choose and a column of noise gets a
different one.

Both probes are reproducible against a live database and drop their own schemas. The
narrower one's recorded plan output also survives in
`.artifacts/in-progress/2026-08-28-ceilings-2-and-3.md`.

**Tenant creation stays inside the existing bypass surface.** It becomes a DDL operation, but
it already runs in `owner_session()`, which invariant 2 lists for tenancy provisioning. The
surface does not widen. A `CREATE TABLE` inside a request is a different failure mode from an
`INSERT` and needs its own handling; that is work, not a new bypass.

### What is open

Recorded here as open rather than assumed, because each one can change the shape of stage 1:

- **Partition count.** Postgres degrades on planning time and lock-table pressure in the
  thousands. One partition per tenant is the clean model and it does not survive an
  installation with ten thousand customers. `HASH` sub-partitioning and
  list-per-large-tenant-with-a-catch-all are the two shapes to measure. Unmeasured.
- **The distribution of tenant sizes**, which decides who actually benefits. The 200 × 1.6M
  sizing is a uniform split assumed by the plan. One tenant holding most of a corpus gets a
  partition the size of the problem.
- **What RLS costs a probe inside a partition.** `index-shape.json` names this as the first
  thing anyone pursuing IVF must measure; it is unmeasured for HNSW inside a partition too.
  Every `index-shape.json` and `dimensions.json` latency was taken against scratch tables
  carrying no policy and no join to `chunks`.
- **Whether ParadeDB's custom scan still executes on a partitioned table.** This one matters
  more than its length here suggests. ADR 0002 records the F18 failure: with the predicate
  left outside the Tantivy query the custom scan does not run, every `paradedb.score(id)` is
  `NULL`, ranking by a NULL score ranks everything equally, and search keeps answering —
  worse, silently. If partitioning reintroduces that, it reintroduces it in the same silent
  direction. It must be tested before `chunks` is partitioned, not after.
- **The aggregate cost of building one HNSW index per partition** at 322.6M. Per-partition
  builds are parallelisable, which is a benefit; the total is a wall nobody has costed. The
  only build figures on disk are at 13,549 vectors (`index-shape.json` `hnsw.*.build_ms`).
- **Migrating a live installation.** Repartitioning is not an `ALTER TABLE`. The path is a new
  partitioned table, a backfill and a swap, and it must be interruptible.

## Not taken

**IVF as a global replacement for HNSW.** Its resident footprint is 17,954× smaller —
49,004,544 bytes of centroids against 818.7 GiB of graph at 322.6M — and at 13,549 vectors it
beats HNSW on the worst question at five of the six list counts swept and ties at the sixth,
0.70–0.80 against HNSW's 0.50 (`index-shape.json` `crossing.fp32`). But matching HNSW's
recall means probing 10% of the lists,
which is 81.9 GiB read per query and, at the report's assumed 2 GB/s sequential, ~44 s
(`index-shape.json` `projection.fp16.sizes.322000000`). It becomes interesting again **inside
a partition**, where 10% is 10% of one tenant — a stage 1 follow-up, not a replacement
decision. Two caveats from the same file bind that follow-up: IVF recall at 13,549 vectors
says nothing about IVF recall at 322M, and k-means is seeded randomly, so `crossing` is the
shape of a boundary and not a threshold.

**Binary quantisation.** Rejected, and the corrected run rejects it harder. Once the arms
actually used an index, the gap per decade at rescore 100 went from +0.0384 to **+0.0606**,
and the extrapolated gap at 300M to **0.3300** — recall@10 ≈ 0.67 (`quantisation.json`
`trend.binary_r100.at_10`; see *Evidence* for what moved and where). 18.84×
is the largest factor available and it is the one that buys least, because it is the only
lever whose damage compounds with the same N everything else is fighting.

**Coarse-to-fine as the global design — deferred, not refused.** 15.65× on its own and 31.27×
combined with `svd_512`, which is 820.2 GiB → 26.2 GiB at a million documents
(`coarse-dims.json` `memory`), and it reproduces the page exactly at this corpus: headline
Recall@8 0.90 and Recall@1 0.6667, unchanged, at 1024 and at 512 components alike, with the
two levers commuting to 5.36e-07. It is deferred because the operating point does not
extrapolate — `open ~ N^1.112` and a fraction that will not fall. Inside a partition that
fraction is affordable; as the global design it is not, and stage 1 delivers most of what it
would buy at a fraction of the risk.

**The measurement that would unblock it** is not another dial sweep. It is whether a hierarchy
— groups of groups — turns that fraction sublinear, since a flat coarse stage demonstrably
does not. Also settled and worth keeping: max pooling is worse than mean at **every one of the
26 dial positions where both ran, with no ties** (`coarse.json` `arms`). A per-dimension
maximum is not a valid embedding; it sits off the manifold the real ones occupy, and cosine
similarity to it stops meaning what the argument for it assumed.

**Dimensions below 512.** `svd_512` costs 2.0 points of index recall@10 (0.9667 → 0.9467)
while leaving the worst question at 0.50 and the count of questions below 0.9 unchanged at
three. At **384 one more question breaks** (three → four) and top-1 preservation falls 0.9333
→ 0.8333. At **256, six more break** (three → nine) and top-1 preservation collapses **0.9333 →
0.6667**. All from `dimensions.json` `arms`. Two related results from the same file: centred
PCA at full width — a rotation that discards nothing — costs 18.7 points (0.9667 → 0.7800),
because centring moves every point and changes every norm and cosine is a function of the
norms; and a random orthonormal basis at 256 is 24.3 points worse than SVD at the same width.
Use uncentred SVD, never centred PCA.

## Evidence

Every figure above comes from a run written to disk under `backend/eval/`, and every one of
them was measured on **the same 42-document, 13,549-passage corpus** unless it says otherwise.
Files cited: `quantisation.json`, `dimensions.json`, `index-shape.json`, `tenant-scale.json`,
`coarse.json`, `coarse-scale.json`, `coarse-dims.json`, `scale.json`, `latency.json`,
`live-recall.json`, and the three structural probes `partition-pruning.sql`,
`partition-rls-policy-pruning.sql` and `partition-rls.sql`.

### A correction happened, and several numbers moved

`quantisation.py` was leaving `enable_indexscan` and `enable_bitmapscan` off for its arms —
`SET LOCAL` lasts to the end of the transaction, not the end of the statement — so every arm
was answered by a sequential scan and the fp32 and fp16 index-recall rows were comparing exact
retrieval against itself. They were necessarily 1.0000, and the report published before the
fix showed both flat at 0.0000 per decade. `quantisation.json` `planner` records the defect,
and `plan_uses_index` on each arm and `truth_is_sequential` under each size now make the claim
checkable rather than asserted.

**Every fp32, fp16 and binary figure in this ADR is the corrected one.** Two documents were
still quoting the superseded pair (+0.0384 per decade, ≈ 0.79 recall at 300M) and are
corrected on this branch: the docstring of migration `0025_halfvec_vector_index.py`, which
also stated fp32 and fp16 index recall as 1.0000 and flat, and the docstring of
`eval/scale.py`. Migration 0025's *decision* — ship fp16, do not ship binary — survives the
correction unchanged, because it rests on fp16 matching fp32 through the same index and not
on either being exact; only the numbers cited for it were void, and its `upgrade()` is
untouched. A shipped migration's SQL is immutable; its prose is not.

One trap the correction leaves behind, recorded because it is the kind that gets rediscovered
the expensive way: **`eval/scale.py` disables the index scan deliberately**, to isolate what a
representation loses from what the graph loses. That is the same line of SQL as the defect,
and in a diff the two are indistinguishable. `scale.py`'s `fp16: 0.0` rows are therefore
*correct* and mean something quite different from `quantisation.json`'s void 1.0000 rows.
Both docstrings now say so.

### Which numbers are projections, and on what assumption

| projected | assumption |
|---|---|
| 820.2 GiB, 2,460.2 GiB, 26.2 GiB at 1M documents | 322.6 passages per document, from this corpus; linear in vectors. `quantisation.json` also carries a `corporate_mix` at 80 passages per document, which is a third of the footprint |
| gap 0.2348 at 322.6M, 0.1322 at 1.6M, +0.10 between them | log-linear fit over four sizes, 2,000 → 13,549 — **0.83 of a decade of data, projected 4.4 decades beyond its top point.** This is the weakest load-bearing assumption in the record |
| `open` 2.06M and 9.97% IO at 322.6M | `coarse-scale.json` `shape.0.9355`, fitted over a synthetic corpus that matches a real one in row count **or** in density, never both. The file states the two readings bracket the answer and that only the *sign* is entitled: the required `open` is not a constant |
| IVF ~44 s per query at 322.6M | `index-shape.json` `settings.assumed_storage` — 2 GB/s sequential, 80 µs random 8 KiB. Explicitly assumptions, not measurements; nothing read from a cold device. The 81.9 GiB is the transferable figure |
| 22 GB resident for ten active tenants | 200 uniform tenants, ten concurrent, fp16 + `svd_512`. `svd_512` is measured but not built |

### What would falsify this

Stated so that the next person can check rather than trust, in the manner of ADR 0002's
superseded section:

- **The recall claim** fails if a partitioned installation at a real size does not show the
  gap shrinking with N. It rests entirely on a fit spanning less than one decade. One
  measurement at 1M passages in a single partition, against exact retrieval, would settle it
  in either direction.
- **The isolation claim** fails the moment a partition exists without `relrowsecurity` and a
  policy. That is what stage 0's enumerating test asserts, and it is the reason no table is
  partitioned before that test is green.
- **The ParadeDB claim is not yet a claim.** Whether the custom scan executes on a partitioned
  table is untested. If it does not, `paradedb.score()` returns NULL and search degrades
  without saying so — the F18 failure ADR 0002 records — and this decision needs amending
  rather than merely scheduling.

`coarse.json`'s `free_at` is `null` for every grouping, which is worth stating plainly: **no
dial position met that file's own bar** of recall@10 = 1.0000 with the worst question at 1.0.
What coarse-to-fine cleared is the end-to-end bar — headline Recall@8 and Recall@1 not below
the exact baseline — and that is the weaker of the two. It does not change the deferral, and
quoting the 15.65× without it would overstate the lever this ADR is declining to build.
