# `svd_512` — projecting the embedding space from 1024 to 512

**Date:** 2026-08-29 · **Stage:** ceiling-3 stage 04, the last one · **Branch:** `perf/svd-512`
**Base:** `integration/scaling` at `b1b9549`
**Spec:** `.artifacts/specs/2026-08-29-ceiling-3-architecture.md` (stage 2 there; stage 04 in
the build order, because it changes the vectors themselves and had to come after the recall
gates of every other stage were green)

Every figure below comes from a run written to disk under `backend/eval/`. Where a number is
a projection from one it says so.

---

## What is already measured, and is not re-derived here

`backend/eval/dimensions.json`, 2026-08-29, `cpu`, 13,549 embeddings, 42 questions split
30 answerable / 12 unanswerable:

| arm | dim | B/vector | index recall@10 (answerable) | worst question | worse by >0.1 |
|---|---|---|---|---|---|
| `identity_1024` | 1024 | 2,729.9 | 0.9667 | 0.50 `identifier-omb-number` | — |
| **`svd_512`** | **512** | **1,365.8** | **0.9467** | **0.50 `identifier-omb-number`** | **0** |
| `svd_384` | 384 | 1,170.5 | 0.9167 | 0.50 | 1 |
| `svd_256` | 256 | 833.2 | 0.8733 | 0.50 | 6 |
| `pca_1024` | 1024 | 2,729.9 | 0.7800 | 0.20 | 15 |

Three things that decide this stage's design and are taken as settled:

- **512 is the floor.** 384 breaks one question; 256 breaks six and takes top-1 preservation
  from 0.933 to 0.667.
- **Uncentred SVD, never centred PCA.** `pca_1024` is a rotation that discards *nothing* and
  still costs 18.7 points, because centring moves every point and changes every norm, and
  cosine is a function of the norms. The cost is the subtraction, not the dimensions.
- **2.00× on the index**, on top of the 3× fp16 of migration 0025 and independent of the
  partitioning of 0026.

---

## The four mechanisms, and what was verified before any of them was built

### 1. The basis is a property of a space, and it lives in the database

`embedding_spaces` already exists so several spaces can coexist during a reindex (RNF-08),
and `(model, version)` is already the space's identity: it is the primary key there, it is
in `chunk_embeddings`' primary key, and it is already the filter `retrieval.search.dense`
emits. The projection therefore attaches to a space rather than becoming a global constant.

- `embedding_space_axes(model, version, component, axis vector(1024))` — one row per output
  dimension, foreign key to `embedding_spaces`, `ON DELETE CASCADE`. No RLS: it describes a
  model, not content, exactly like `embedding_spaces` (`TABLES_WITHOUT_RLS`).
- `embedding_spaces.source_dimension` — non-null exactly when the space is projected.
- `embedding_spaces.basis_digest` — sha256 over the axes as stored, so two installations can
  be asked whether they are running the same fit.

The 512 space is `("BAAI/bge-m3", "2")`. It is a new **version** of the same model, because
that is what it is: the same embeddings, read in a different basis.

### 2. Mixing bases is a Postgres error, not a convention

This is the part that mattered more than the projection, and it is settled by the type
system rather than by review discipline.

`chunk_embeddings.embedding` loses its `vector(1024)` typmod and becomes bare `vector`, so a
row's dimension is whatever its space says. Each space then carries its own **partial
expression** HNSW index, `((embedding_half::halfvec(k)) halfvec_cosine_ops) WHERE
embedding_version = '…'`.

Measured on a scratch database before anything was written — `backend/eval/svd-basis.sql`,
reproducible:

| what was asked | what happened |
|---|---|
| 512-d query, 512 space | `Index Scan using probe_v2` |
| 1024-d query, 1024 space | `Index Scan using probe_v1` |
| **1024-d query aimed at the 512 space** | `ERROR: different halfvec dimensions 1024 and 512` |
| **512-d query aimed at the 1024 space** | `ERROR: different halfvec dimensions 512 and 1024` |
| **right-sized query, wrong space in the filter** | `ERROR: expected 512 dimensions, not 1024` |

Every way of pairing a vector with the wrong basis that could be constructed is a hard error
at the first row touched. **Nothing ranks.** That is the failure direction this repository
already chose for invariant 1: the mistake returns an error rather than a plausible answer.

Two supports underneath it, so the type check is never the only thing standing:

- **There is exactly one copy of the basis, and it is the one both sides read.** The
  projection is a SQL function, `zenith_project(v, model, version)`, which reads the axes of
  the space it is given. Query vectors and stored vectors are projected by *the same rows of
  the same table*. There is no matrix in Python, in a file or in a migration body to drift.
- **The caller names the space once.** `dense()` stops taking `(embedding, model, version)`
  as three independent arguments — that was the hole — and takes the vector plus one `Space`
  loaded from the database. The same value selects the basis and filters the rows.

`zenith_project` in SQL rather than numpy is also forced: `backend/pyproject.toml` keeps
numpy in the `eval` dependency group on purpose, because `Dockerfile.backend` builds with
`uv sync --no-dev` and the shipped image must not grow for a measurement sweep. numpy is not
in the api container and this stage does not put it there.

### 3. What the projection costs, measured

Scratch database, same Postgres, 1024 → 512:

| | measured |
|---|---|
| one query vector, warm (what `/search` pays) | **0.79 – 0.87 ms** |
| 13,549 vectors (what a reindex of this corpus pays) | **13.9 s**, ~1.02 ms/vector |

0.87 ms against a live median of 941 ms is noise. The backfill figure is the honest one to
carry forward: at 322M passages it is ~91 hours single-threaded, and it parallelises per
partition, which is the same shape as the HNSW build cost the spec already lists as uncosted.

### 4. The reindex path

**Both spaces are resident at once — proven, not assumed** (rows A and B of the table above:
two partial HNSW indexes on one column, each used for its own space). `chunk_embeddings`'
primary key already carries `(embedding_model, embedding_version)`, so a chunk holds its 1024
row and its 512 row simultaneously.

1. Register `("BAAI/bge-m3", "2")` with `status = 'building'` and insert its 512 axes.
2. Backfill per tenant, **with the tenant bound as a parameter.** A write does not prune on
   `zenith_current_tenant()` — `docs/partitioning-unpruned-surface.md`: 19 locks against
   2,059 for the identical statement — so the backfill takes the tenant as a value or it
   opens every partition for writing.
3. Build the partial index for version `2`.
4. Flip `2` to `active`, `1` to `retired`.
5. Delete the retired rows when the operator is satisfied. Nothing forces this, and the
   1024 space is a working rollback until it happens.

Interruptible because step 2 is per tenant and idempotent on the primary key. No
re-embedding: the projection reads the stored fp32 `embedding`, which is why this is cheap
against a model change and why `embedding` staying fp32 (migration 0025's decision) pays for
itself a second time.

---

## The bars, written before the run

1. `backend/eval/live-recall.json` must not fall: **headline Recall@8 0.9000, Recall@1
   0.6667**, on the real installation, measured before and after.
2. The worst single question is reported, not only the mean. `dimensions.json` says it should
   be unchanged at 0.50, `identifier-omb-number`.
3. The 31 answerable questions are scored separately from the 12 unanswerable ones.
4. `make check` exit code captured directly, on this branch **and on the merge with
   `integration/scaling`**.
5. `alembic upgrade head` → `downgrade -1` → `upgrade head`, corpus intact at every step.
6. A test that proves the mixing gate *fails loudly*, against a real mismatch it constructs.

A failed bar stays in this file.

---

## Outcome — 2026-08-30

Every bar above was met. Three of them moved from "declared" to "measured" and two claims in
the plan turned out to be wrong; both corrections are below rather than edited away.

### The gates

| gate | outcome |
|---|---|
| `make check` on the branch, exit code captured directly | **EXIT=0** (after two real failures, below) |
| `make check` on the merge with `integration/scaling` | **EXIT=0** |
| `alembic upgrade head` → `downgrade -1` → `upgrade head` | **all three exit 0**, 13,549 embeddings / 13,549 chunks / 42 documents intact at every step, column types flipping `vector(1024)` ↔ `vector` and back, 129 HNSW relations throughout |
| `live-recall.json` must not fall | **held.** Recall@8 **0.9000**, Recall@1 **0.6667** on all three arms |
| worst single question | **unchanged.** The same three questions miss in every arm: `boe-bank-rate`, `cross-platform-obligations`, `table-form-1040-status` |
| answerable scored apart from unanswerable | `dimensions.json`'s 30/12 split, quoted and not re-derived; `live.py` scores its own 20 headline questions separately from the 30 |

### The three live arms

Measured over HTTP through a real deployment. The first is the installation; the other two are
a scratch copy of it at 0027 served by an api built from this branch, which is what separates
*the mechanism* from *the projection*.

| arm | Recall@8 | Recall@1 | mean rank | median / p95 |
|---|---|---|---|---|
| the real installation, 0026, identity 1024 | 0.9000 | 0.6667 | 1.37 | 839 / 1090 ms |
| this branch, 0027, identity 1024 still active | 0.9000 | 0.6667 | 1.37 | 872 / 1247 ms |
| **this branch, 0027, `svd_512` active** | **0.9000** | **0.6667** | **1.37** | **861 / 1160 ms** |

Identical, including which questions miss. **An identical number is also what a measurement
that did not happen looks like**, so it is not the evidence — this is: across five real HTTP
searches with the statistics reset before and force-flushed after, `svd_512`'s partition
indexes took **5 index scans on exactly 1 partition, reading 250 tuples** (`CANDIDATES` 50 × 5,
so the dense half got every candidate it asked for), and the retired 1024 index took **0**. The
live path goes through the projected index, prunes to one partition, and never ranks across
both spaces.

### Two corrections to the plan above

**1. There is no `zenith fit-basis` command, and there cannot be one yet.** The plan promised
it. Fitting is an eigendecomposition of a 1024×1024 matrix and needs numpy, which is
deliberately absent from the shipped image — the same constraint that forced the projection
into SQL forbids the fit from living in the CLI. *Applying* a basis needs nothing but
Postgres; *fitting* one does not. `eval/svd_512.py` is the whole of the tooling today, and
migration 0027's docstring now names the gap instead of pointing at a command that does not
exist. Making it shippable means deciding where numpy lives, and that decision is not taken
here.

**2. The factor is 1.90×, not 2.00×.** `dimensions.json`'s 2,729.9 → 1,365.8 was measured on
unpartitioned arms. On the real corpus at 128 partitions it is **2,883.4 → 1,518.8 = 1.90×**
(`svd-512.json`): both widths pay 128 sets of page overhead and the smaller one pays
proportionally more. 1.90× is the figure a partitioned installation should be sized on.

### Three failures worth keeping

`make check` returned **EXIT=2 twice** before it returned 0 — once on `format-check`, once on
`web-types` with `tsc: command not found`, which was this worktree never having had `npm ci`
run in it. Neither would have been visible through a `tail`.

The sweep's first run failed two of its four bars, and **both failures were in the harness and
both looked exactly like product regressions**: the plan check had no `enable_seqscan`/
`enable_sort` off and read `Limit → Sort` at a size where the planner is right to sort; and
the 512 index's partition copies were never renamed, so the footprint read 0 bytes per vector
and the plan verdict matched against a name the index did not have.

And the first attempt to prove index use slept two seconds, read zeros from
`pg_stat_user_indexes`, and looked precisely like the silent sequential-scan regression this
stage had to avoid. It was the statistics collector not having flushed.
