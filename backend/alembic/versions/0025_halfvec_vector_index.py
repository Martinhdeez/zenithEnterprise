"""The vector index becomes fp16, and the fp32 vectors stay where they are.

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-28

HNSW is the structural ceiling on how much corpus one installation can hold, because the
graph wants to be resident. `eval/quantisation.json` (2026-08-28, 13,549 vectors, `m=16,
ef_construction=64`, 30 real embedded questions) measured what each representation costs:

| variant                    | bytes/vector | ratio | index recall@10 vs exact | trend 2k->13.5k |
|---|---|---|---|---|
| fp32 `vector_cosine_ops`   | 8,188.4      | 1.00x | 1.0000                   | flat            |
| fp16 `halfvec_cosine_ops`  | 2,729.9      | 3.00x | 1.0000                   | flat            |
| binary `bit_hamming_ops`   | 434.7        | 18.84x| 0.9533 at rescore 100    | +0.0384/decade  |

At this corpus's 322.6 passages per document, a million documents is 2,460.2 GiB of fp32
index and 820.2 GiB of fp16. That is the largest capacity change available at no measured
cost: on the full corpus fp16's *worst* query is 1.0000 at both depths, end to end it scores
the same headline Recall@8 (0.90) and Recall@1 (0.6667) as fp32, it queries no slower (31.97
ms median against 34.31) and builds in 2,907 ms against 4,938.

**Only fp16 ships.** Binary's gap to exact widens with N — +0.0384 per decade at depth 10 —
and a number measured at 13.5k that degrades with corpus size is the one number a report at
this scale cannot settle. It is under separate investigation and this migration does not
prepare for it beyond point 2 below.

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
