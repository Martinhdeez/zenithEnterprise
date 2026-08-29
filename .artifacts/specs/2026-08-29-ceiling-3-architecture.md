# Ceiling 3 — the architecture, and the order it gets built in

**Date:** 2026-08-29 · **Status:** plan, stage 0 in progress

Every figure here comes from a run under `backend/eval/`. Where a number is a projection it
says so and carries its assumption.

## What the measurements actually said

Four levers were measured against the vector index's memory ceiling. Three of them shrink
the index. One of them changes the problem.

| lever | factor | verdict | evidence |
|---|---|---|---|
| fp16 | 3.00× | **shipped**, migration 0025 | `live-recall.json` unchanged across the migration; indistinguishable from fp32 through the same index |
| `svd_512` | 2.00× | measured, not built | −2.0 points index recall@10, worst question unchanged, **no question worse by >0.1** |
| coarse-to-fine | 15.6× | measured, **does not scale alone** | page-identical at 13,549; `open ~ N^1.112`, IO stuck at 3–6% of the corpus |
| IVF instead of HNSW | 18,000× resident | measured, **no** as a global replacement | reaches HNSW's recall reading 10% of the corpus — 82 GiB and ~44 s at 322M |

And one finding that reframes all four. With the planner defect fixed, **fp32 — uncompressed
— loses 4 points of recall@10 to HNSW at `ef_search = 100`, and both fp32 and fp16 degrade
with corpus size** at +0.0445 and +0.0386 per decade. The earlier report showed them flat at
0.0000 and hid this completely.

**The graph has its own scaling wall and it arrives before the storage one.** Compression
does not touch it. A bigger machine does not touch it.

## The conclusion that follows

Every "make the index smaller" lever hits the same wall. With 322M passages in **one** index,
either it is resident or each query reads a fraction of it — and a fraction of 322M is
millions of vectors, whatever the bytes per vector.

**Partitioning by tenant is the only lever that changes N**, and N is what every other number
here depends on. It is therefore not one option among four. It is the one that makes the
other three work:

- A tenant holding 1.6M passages searches a 1.6M graph. HNSW's degradation depends on N, so
  every query moves back down that curve by two decades.
- Coarse-to-fine's stubborn 3–6% applies to 1.6M, not 322M: **64,000 passages read, not
  thirteen million.** The IO budget absorbs that.
- IVF's 10% becomes 160,000 passages, and IVF's resident footprint is centroids only.
- Resident memory becomes the *active* tenants rather than the corpus.

Sizing at 1M documents, 322.6 passages each, 200 tenants, ten searching at once, on
fp16 + `svd_512` at a measured 1,365.8 B/vector:

    per tenant   1.6M × 1,365.8  =  2.2 GB
    ten active                   =  22 GB resident

That is a comfortable fit in 128 GB, **without** coarse-to-fine, which becomes optional
margin rather than a requirement. It is also the least invasive path: coarse-to-fine changes
what retrieval means, and partitioning does not.

## Stage 0 — the prerequisite, and it is not negotiable

**Partitions do not inherit the parent's RLS policy.** Demonstrated, not assumed
(`partition-rls.sql`):

    relname | rls_enabled | own_policies
    chunks  | t           | 1
    p2      | f           | 0

With `zenith.tenant_id` set to tenant 1, a query against the parent correctly returns tenant
1's rows. A query against partition `p2` returns **tenant 3's rows in full**.

Nothing grants a partition today, so nothing is exposed today. But
`GRANT ... ON ALL TABLES IN SCHEMA public TO zenith_app` would, and so would any migration
written by somebody who assumed inheritance — which is the assumption this stage exists to
kill. It is invariant 1 broken by a schema change, in a product whose entire claim is that
isolation is enforced by the database and not by the discipline of whoever writes the query.

It also fails in the direction this repository refuses: the ordinary path keeps working, so
nothing surfaces.

**What stage 0 delivers, before any table is partitioned:**

1. `ENABLE ROW LEVEL SECURITY` and the policy template on **every** partition, not only the
   parent, so the failure is closed even when a grant leaks. Applied by the same helper that
   creates a partition, so it cannot be forgotten by hand.
2. A test that enumerates every partition of every RLS-protected table and asserts both, in
   the shape of the existing `SECURITY DEFINER` allowlist test — a list somebody has to
   justify, not a convention somebody has to remember.
3. `partition-rls.sql` kept under `backend/eval/` as the reproducible demonstration, beside
   `partition-pruning.sql`.

## Stage 1 — partition `chunks` and `chunk_embeddings` by tenant

The structural change. Runtime pruning is already verified — `partition-pruning.sql` shows
`Subplans Removed: 7` of 8 with the key coming from `zenith_current_tenant()`, which is
`STABLE` and therefore cannot prune at plan time — in all three shapes: the direct query, a
`PREPARE`/`EXECUTE` pair, and the query with the vector `ORDER BY` on top.

Open questions this stage has to answer rather than assume:

- **Partition count.** Postgres degrades on planning time and lock-table pressure in the
  thousands. A partition per tenant is the clean model and it does not survive an
  installation with ten thousand customers. `HASH` sub-partitioning, or list-per-large-tenant
  with a shared catch-all, are the two shapes to measure.
- **Tenant creation becomes DDL.** It happens in `owner_session` today, which is already the
  bypass path used for tenancy provisioning, so the surface does not widen — but a `CREATE
  TABLE` inside a request is a different failure mode from an `INSERT` and needs its own
  handling.
- **Migrating a live installation.** Repartitioning an existing table is not an
  `ALTER TABLE`. The path is a new partitioned table, a backfill, and a swap, and it must be
  interruptible.
- **One HNSW index per partition.** Build time is per-partition and parallelisable, which is
  a benefit; the aggregate build cost at 322M is a wall nobody has costed, and IVF's k-means
  sample alone is ~4.5 GB at 32,768 lists.
- **What RLS costs a probe** inside a partition. `index-shape.json` names this as the first
  thing anyone pursuing IVF must measure, and it is unmeasured for HNSW too.

## Stage 2 — `svd_512`

Independent of stage 1 and worth 2× on its own. Not free and not expensive: −2.0 points of
index recall@10, the worst question unchanged at 0.50, and not one question worse by more
than 0.1. 384 is the boundary — one question breaks — and 256 is not available: six break
and top-1 preservation collapses from 0.933 to 0.667.

Two things make this more than a migration, and they are why it is stage 2 rather than
stage 0:

- The basis has to be **fitted, stored and versioned**. `embedding_spaces` already exists for
  exactly this — several spaces coexisting during a reindex — so the projection is a property
  of a space, not a global constant.
- **Query vectors must go through the same basis.** A stored vector projected with one basis
  and a query vector projected with another is confident nonsense, which is the failure ADR
  0002 already names for mixing embedding models.

Use **uncentred SVD, never centred PCA.** Centring is not a rotation: it moves every point
and changes every norm, and cosine is a function of the norms. Measured, `pca_1024` — a
rotation that discards *nothing* — costs **18.7 points**. Textbook PCA would have charged
that to the dimensions and reported that reduction is expensive. Random projection is 24
points worse than SVD at 256 and is not the simpler thing to ship.

## Stage 3 — coarse-to-fine, deferred with a reason

15.6× resident, and it reproduces the page exactly at this corpus: headline Recall@8 0.9000
and Recall@1 0.6667, unchanged, on `svd_512` as well as at 1024. Combined with `svd_512` the
measured factor is **31.27×**, or 820.2 GiB → 26.2 GiB at a million documents.

It is deferred because **the operating point does not extrapolate**. `open ~ N^1.112`: a
fixed fraction wearing a count's clothing, holding a stubborn 3–6% of passages read at every
size measured, because the mean number of distinct groups holding the true top ten climbs
from 5.9 to 9.94. Inside a partition that fraction is affordable; as the global design it is
not, and stage 1 delivers most of what it would buy at a fraction of the risk.

**The measurement that would unblock it** is not another dial sweep. It is whether a
hierarchy — groups of groups — turns that fraction sublinear, since a flat coarse stage
demonstrably does not.

Also settled and worth keeping: **max pooling is worse than mean at every one of the 26 dial
positions where both ran**, with no ties. A per-dimension maximum is not a valid embedding;
it sits off the manifold the real ones occupy, and cosine similarity to it stops meaning what
the argument for it assumed.

## Not in this plan, and why

**IVF as a global replacement.** Its resident footprint is 18,000× smaller — 45.6 MB against
819 GiB at 322M — and it beats HNSW on the worst question, 0.70–0.80 against 0.50. But
matching HNSW's recall reads 10% of the corpus: 82 GiB and roughly 44 s per query. The memory
saved comes back as page cache with interest. It becomes interesting again **inside a
partition**, where 10% is 10% of one tenant, and that belongs to a stage 1 follow-up rather
than to a replacement decision.

**Binary quantisation.** Rejected, and the corrected measurement rejects it harder: the gap
per decade at rescore 100 went from +0.0384 to +0.0606 once the arms actually used an index,
and the extrapolated gap at 300M from 0.2137 to 0.3300.

## One operational finding that belongs to nobody's stage

`zenith-tei-rerank-1` was found **OOM-killed** (exit 137) mid-session and restarted. Without
it search still answers, roughly fifteen points of recall worse, and says nothing — the
degradation `docker/docker-compose.yml` documents and `demo-check` exists to catch. It fell
over under memory pressure on a developer machine; at the corpus sizes this plan is for, it
will fall over under load. It needs a memory limit, a restart policy and an alarm, and none
of that is in any stage above.
