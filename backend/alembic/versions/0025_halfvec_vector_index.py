"""The vector index becomes fp16, and the fp32 vectors stay where they are.

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-28

HNSW is the structural ceiling on how much corpus one installation can hold, because the
graph wants to be resident. `eval/quantisation.json` (13,549 vectors, `m=16,
ef_construction=64`, 30 real embedded questions; measured 2026-08-28, re-run 2026-08-29)
measured what each representation costs:

| variant                    | bytes/vector | ratio | index recall@10 vs exact | trend/decade |
|---|---|---|---|---|
| fp32 `vector_cosine_ops`   | 8,188.4      | 1.00x | 0.9600                   | +0.0445      |
| fp16 `halfvec_cosine_ops`  | 2,729.9      | 3.00x | 0.9600                   | +0.0386      |
| binary `bit_hamming_ops`   | 434.7        | 18.84x| 0.9333 at rescore 100    | +0.0606      |

## The figures above are corrected, and the ones this migration shipped against were void

`quantisation.py` was leaving `SET LOCAL enable_indexscan = off` in force for its arms. It is
set deliberately, to make the exact-retrieval ground truth a sequential scan; `SET LOCAL`
lasts to the end of the transaction and not the end of the statement, so it leaked into every
arm measured afterwards. Each "index" arm was therefore a sequential scan compared against a
sequential scan — the same computation on both sides — which is why fp32 and fp16 both
reported recall 1.0000 and a flat trend. They were being compared against themselves. The
re-run restores the settings, additionally turns `enable_seqscan` off because restoring them
was not enough on these scratch copies, and records `plan_uses_index` on each arm and
`truth_is_sequential` under each size, so the claim is checkable rather than asserted.

**The decision this migration takes survives the correction unchanged.** It never rested on
either representation being exact — it rests on fp16 being indistinguishable from fp32
through the same index at the same `ef_search`, and the corrected run says that more clearly
than the void one could: identical recall@10 (0.9600 both), identical worst query (0.50 at
depth 10, 0.72 at depth 50), and `eval/live-recall.json` unchanged across the migration at
headline Recall@8 0.90 and Recall@1 0.6667. What the correction revealed is the size of the
*graph's* own approximation error, which fp32 pays in full and which no representation
avoids. That is a different problem with a different lever, and it is ADR 0009's subject.

`eval/scale.py` measures the same three representations with the index scan disabled *on
purpose*, to isolate what a representation loses rather than what the graph loses. In a diff
that looks identical to the defect above and means the opposite thing.

## What it buys

At this corpus's 322.6 passages per document, a million documents is 2,460.2 GiB of fp32
index and 820.2 GiB of fp16. That is the largest capacity change available at no cost
measured against fp32: end to end it scores the same headline Recall@8 (0.90) and Recall@1
(0.6667), it queries no slower (0.768 ms median against 0.864) and builds in 3,004 ms against
4,873.

**Only fp16 ships.** Binary's gap to exact widens with N — +0.0606 per decade at depth 10,
which the correction made worse rather than better — and a number measured at 13.5k that
degrades with corpus size is the one number a report at this scale cannot settle. It is under
separate investigation and this migration does not prepare for it beyond point 2 below.

## Why `embedding` keeps its type, and a second representation is added beside it

`ALTER COLUMN embedding TYPE halfvec(1024)` is the obvious migration and it is the wrong
one. Three reasons, in the order they bind:

1. **It is lossy and therefore irreversible in practice.** fp16 rounds. A `downgrade()` back
   to `vector(1024)` would restore the *type* and not the *values*, so the schema would say
   the rollback worked while the data quietly differed from what the migration was handed. A
   downgrade that returns different numbers than it was given is not a working downgrade, and
   `CONTRIBUTING.md`'s definition of done requires one that is. The `downgrade()` below is
   genuinely exact — it rebuilds an index and drops a derived column, and it can be, precisely
   because the fp32 vectors were never touched.

2. **Exact rescoring needs the fp32 vectors.** Any two-stage retrieval — the binary path, if
   it is ever adopted — generates candidates from a compressed index and reorders them against
   full-precision vectors; that is exactly what `quantisation.py`'s `binary_r*` variants do.
   Discarding fp32 forecloses the design that would make the cheapest representation usable.

3. **Disk is not the constraint; memory is.** The index is what has to stay resident. The heap
   gains 2,048 bytes per row (TOASTed, like the fp32 vector beside it) and that is the trade
   being made deliberately, not an oversight: the resident structure shrinks 3x and the
   non-resident one grows by half.

## `GENERATED ALWAYS AS ... STORED`, and it was checked rather than assumed

Postgres requires a generation expression to be immutable, and pgvector's `vector -> halfvec`
cast is. Verified on this stack — pgvector 0.8.0, Postgres 17.5 — before the column was
written:

    CREATE TEMP TABLE probe (v vector(4),
        h halfvec(4) GENERATED ALWAYS AS (v::halfvec(4)) STORED);

Migration 0022 uses the same construct for `chunks.unlabelled`, so the pattern is established
in this schema. It is the right one here for the same reason it was there: the derived value
cannot drift from its source, because there is no write path that could make it. A plain
column maintained by the ingestion write path would have to be kept in step by every future
writer — the requeue path, a reindex, a hand-repaired row — and the failure mode of missing
one is a passage that is silently unreachable by dense search while `chunk_embeddings` looks
complete.

A generated column is not writable, which is a property rather than a cost: `pipeline.py`
inserts `embedding` and SQLAlchemy omits `Computed` columns from the INSERT, so nothing in
the write path changes.

## Rollout

`CREATE INDEX` is **not** `CONCURRENTLY`: alembic runs migrations in a transaction and
`CONCURRENTLY` cannot. On an installation large enough for the lock to matter, build
`ix_chunk_embeddings_hnsw_half` by hand outside the migration first — `IF NOT EXISTS` is what
makes running this afterwards a no-op — and note that the generated column is added by an
`ALTER TABLE` that rewrites the table, which takes its own lock and cannot be deferred the
same way.

`maintenance_work_mem` is raised for this transaction only. The default on the smallest
supported machine is 64 MB, at which HNSW construction spills and crawls; 512 MB is what
`quantisation.py` builds its subsets with, so the build time above is the one this produces.
`SET LOCAL` keeps it inside the migration's transaction rather than changing the installation.

The old index is dropped **after** the new one is built, so the transaction never leaves the
table without a vector index — a failure between the two rolls back to the fp32 index intact.
"""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

#: The production index's parameters, unchanged. This migration changes the representation
#: and nothing else; a differently-tuned index would make the measurement above inapplicable
#: to the thing it produced.
M = 16
EF_CONSTRUCTION = 64

DIMENSION = 1024


def upgrade() -> None:
    op.execute("SET LOCAL maintenance_work_mem = '512MB'")
    op.execute(
        f"""
        ALTER TABLE chunk_embeddings ADD COLUMN embedding_half halfvec({DIMENSION})
          GENERATED ALWAYS AS (embedding::halfvec({DIMENSION})) STORED
        """
    )
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS ix_chunk_embeddings_hnsw_half ON chunk_embeddings
          USING hnsw (embedding_half halfvec_cosine_ops)
          WITH (m = {M}, ef_construction = {EF_CONSTRUCTION})
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_chunk_embeddings_hnsw")


def downgrade() -> None:
    """Exact, because `chunk_embeddings.embedding` was never written to.

    The fp32 index is rebuilt from the same column, with the same parameters, that built it
    in 0001. The only thing that does not come back is the physical layout of the old graph,
    which no query can observe.
    """
    op.execute("SET LOCAL maintenance_work_mem = '512MB'")
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS ix_chunk_embeddings_hnsw ON chunk_embeddings
          USING hnsw (embedding vector_cosine_ops)
          WITH (m = {M}, ef_construction = {EF_CONSTRUCTION})
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_chunk_embeddings_hnsw_half")
    op.execute("ALTER TABLE chunk_embeddings DROP COLUMN IF EXISTS embedding_half")
