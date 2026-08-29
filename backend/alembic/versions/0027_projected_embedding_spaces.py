"""A space may carry a projection, and a vector's width becomes the space's business.

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-29

Stage 04 of ceiling 3, and the last one. It is worth **2.00x on the vector index**, on top of
the 3x fp16 of migration 0025 and independent of the partitioning of 0026:
`eval/dimensions.json` measures 1,365.8 bytes per vector at 512 dimensions against 2,729.9 at
1,024, on the same corpus, both freshly built.

It is deliberately last, because it is the only stage that **changes the vectors themselves**
and would have contaminated the recall gate of every stage before it.

## What is bought, and what it costs

`eval/dimensions.json`, 13,549 embeddings, 30 answerable questions scored apart from the 12
unanswerable ones, ground truth an exact fp32 sequential scan with every index refused:

| arm | dim | B/vector | index recall@10 | worst question | worse by >0.1 |
|---|---|---|---|---|---|
| `identity_1024` | 1024 | 2,729.9 | 0.9667 | 0.50 `identifier-omb-number` | - |
| **`svd_512`** | **512** | **1,365.8** | **0.9467** | **0.50 `identifier-omb-number`** | **0** |
| `svd_384` | 384 | 1,170.5 | 0.9167 | 0.50 | 1 |
| `svd_256` | 256 | 833.2 | 0.8733 | 0.50 | 6 |

Two points of index recall@10, the worst question unchanged, and **not one question worse by
more than 0.1**. 512 is the floor: 384 breaks one question, and 256 breaks six while top-1
preservation collapses from 0.933 to 0.667.

## Uncentred SVD. Never centred PCA. This is the load-bearing sentence.

`pca_1024` is a rotation onto a full-rank basis — it discards **nothing** — and it costs
**18.7 points** of index recall@10, 0.7800 against 0.9667, with 15 questions worse by more
than 0.1. Centring is not a rotation. It moves every point and changes every norm, and cosine
similarity is a function of the norms, so a centred index ranks by a different metric and
returns different neighbours at *full width*.

Textbook PCA would have charged that loss to the dimensions and reported that reduction is
expensive. It is not. The subtraction is. Anything that later re-fits this basis has to fit
the eigenbasis of the uncentred second moment `X'X / n`, which is what `eval/dimensions.py`
does and what `zenith fit-basis` writes.

Random projection is a further 24 points worse than SVD at 256 and is not the simpler thing
to ship.

## The schema this migration lands, and why each piece is where it is

**1. The basis is a property of a space.** `embedding_spaces` has existed since 0001 for
exactly this reason — several spaces coexisting during a reindex, RNF-08 — and `(model,
version)` is already the space's identity everywhere that matters: it is the primary key
there, it is inside `pk_chunk_embeddings`, and it is already the filter `search.dense`
emits. So `embedding_space_axes` hangs off that key, and a projection cannot exist without a
space to belong to. It carries no RLS for the same reason `embedding_spaces` carries none: it
describes a model, not content.

**2. A vector's width stops being a fact about the column.** `embedding` loses its
`vector(1024)` typmod, `embedding_half` loses its `halfvec(1024)`, and the HNSW index becomes
one **partial expression index per space**, whose predicate is the space filter the query
already carries. Both spaces are therefore resident and separately indexed at the same time,
which is the whole point of a reindex that does not take the installation down.

**3. Mixing two bases stops being possible.** This is the part that mattered more than the
projection, and `eval/svd-basis.sql` demonstrates it rather than asserting it. Once a row's
width is its space's, pgvector's own type check stands between a query vector and the wrong
basis:

    unprojected 1024-d query, 512 space -> ERROR: different halfvec dimensions 1024 and 512
    projected 512-d query, 1024 space   -> ERROR: different halfvec dimensions 512 and 1024
    right width, wrong space filtered   -> ERROR: expected 512 dimensions, not 1024

A stored vector projected with one basis and a query vector projected with another is
confident nonsense — the failure ADR 0002 already names for mixing embedding models — and
this makes it a 500 instead. That is invariant 1's inversion applied to the vector half: the
mistake returns an error, never a plausible answer.

**4. The projection runs in the database, and there is exactly one copy of the basis.**
`zenith_project(v, model, version)` reads the axes of the space it is handed, so query
vectors and stored vectors are projected by the same rows of the same table. There is no
matrix in Python, in a file, or in this migration's body to drift out of step with the data.

It is also the only option. `backend/pyproject.toml` keeps numpy in the `eval` dependency
group deliberately, because `Dockerfile.backend` builds with `uv sync --no-dev` and the
shipped image is not to grow for a measurement sweep; numpy is confirmed absent from the api
container. Measured in `eval/svd-basis.sql`: **0.79-0.87 ms** to project one query vector
warm, against a live search median of 941 ms.

`STABLE` and not `IMMUTABLE`, because it reads a table. That is correct and it is also why
the projection is done in its own round trip rather than inline in the `ORDER BY` — a
`STABLE` call there is not a constant, and pgvector's index scan wants one. The plan is
checked, not assumed: `app/features/retrieval/tests/test_vector_index.py`.

## What this migration does *not* do

It does not project anything. It lands the mechanism and leaves the 1024 space active and
untouched, because a migration that silently reindexed a corpus would be an outage disguised
as an upgrade — and because the projection is per tenant, interruptible, and a **write**,
which on a partitioned table is the operation that does not prune (`docs/partitioning-
unpruned-surface.md`: 19 locks against 2,059 for the identical statement, differing only in
whether the tenant is a bound parameter). `zenith fit-basis` and `zenith reproject` are the
path, and `downgrade` therefore has a corpus to go back to.
"""

from alembic import op
from app.core.config import settings

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

#: 0025's parameters and 0001's width. Copied rather than imported, because a migration
#: describes the schema at its own revision: if a later one changes the model, this file must
#: keep producing what revision 0027 produced.
M = 16
EF_CONSTRUCTION = 64
SOURCE_DIMENSION = 1024

#: The space this installation is running when 0027 arrives, and the only one it has. Named
#: here because the partial index needs a literal predicate — an index cannot be built over
#: "whatever is active" — which is the one place a space identity has to be written down
#: rather than read.
SHIPPED_MODEL = "BAAI/bge-m3"
SHIPPED_VERSION = "1"

MODULUS = settings.partition_modulus

#: The same floor 0026 refuses to start below, and for the same reason, at a lower peak.
#:
#: This migration is not a rewrite of the corpus, but `ALTER TABLE ... ALTER COLUMN TYPE` on
#: a partitioned parent takes an `ACCESS EXCLUSIVE` lock on every partition and on every
#: index over it, and it does so in one transaction. That is the same `9P + 9` shape
#: `eval/modulus-cost.json` measured for a search, taken at the strongest lock level rather
#: than the weakest.
#:
#: Kept as `4 * MODULUS` rather than a constant for the reason 0026 gives: the locks are
#: linear in the modulus, so a constant would silently wipe out the margin the first time
#: somebody doubled it.
MIN_LOCKS_PER_TRANSACTION = 4 * MODULUS

_LOCK_PREFLIGHT = """
DO $do$
DECLARE configured int := current_setting('max_locks_per_transaction')::int;
BEGIN
    IF configured < %(minimum)s THEN
        RAISE EXCEPTION
            'max_locks_per_transaction is %%, and migration 0027 needs at least %(minimum)s',
            configured
        USING HINT = 'Retyping a column on a partitioned table locks every one of the '
                     '%(modulus)s partitions and every index on it, in one transaction. Set '
                     'it in docker/docker-compose.yml and restart Postgres -- restart, not '
                     'reload: it is a startup parameter, so a correct value in the '
                     'repository and a wrong one in the running database look identical '
                     'from the code.';
    END IF;
END $do$
"""

#: The projection: `k` inner products against the axes of one space.
#:
#: `<#>` is pgvector's *negative* inner product, hence the negation. The axes are orthonormal
#: by construction, so this is the projection `eval/dimensions.py` measured — `X @ B[:, :k]`,
#: with no mean subtracted, which is the whole argument of this migration's third section.
#:
#: Returns bare `halfvec`, deliberately. The caller casts to the width it expects, and that
#: cast is what turns a mismatched basis into an error instead of a ranking.
#:
#: `NULL` for a space with no axes rather than an exception, so that the 1024 space -- which
#: has none, being the identity -- reads as "nothing to project" at the one call site that
#: has to distinguish them, instead of as a failure.
#:
#: `SECURITY INVOKER`, which is the default and is stated because the alternative is the
#: third class of RLS bypass CLAUDE.md warns no grep finds. There is nothing to bypass here:
#: `embedding_space_axes` carries no policy, exactly like `embedding_spaces`.
_PROJECT = """
CREATE FUNCTION zenith_project(v vector, model text, version text)
RETURNS halfvec
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY INVOKER
AS $fn$
    SELECT (array_agg((a.axis <#> v) * -1 ORDER BY a.component))::real[]::vector::halfvec
    FROM embedding_space_axes a
    WHERE a.model = zenith_project.model AND a.version = zenith_project.version
$fn$
"""


def _require_lock_table() -> None:
    op.execute(_LOCK_PREFLIGHT % {"minimum": MIN_LOCKS_PER_TRANSACTION, "modulus": MODULUS})


def upgrade() -> None:
    _require_lock_table()
    # Retyping the column rewrites the table, and rebuilding the HNSW index spills at the
    # 64 MB default. `SET LOCAL` keeps both inside this transaction, as 0026 does.
    op.execute("SET LOCAL statement_timeout = 0")
    op.execute("SET LOCAL maintenance_work_mem = '512MB'")

    _describe_the_projection()
    _widen_the_vector_columns()
    _index_each_space_separately()


def _describe_the_projection() -> None:
    """Where a basis lives, and what makes a space a projected one.

    `source_dimension` is non-null exactly when the space projects, which makes "is this
    space a projection" a column rather than a join — the question `search.dense` asks on
    every request.

    `basis_digest` is how two installations are asked whether they are running the same fit.
    Two bases fitted from different corpora are both valid, both orthonormal, and produce
    silently different neighbours; the digest is the only cheap way to tell them apart, and
    it is what `zenith diagnose` compares.
    """
    op.execute("ALTER TABLE embedding_spaces ADD COLUMN source_dimension integer")
    op.execute("ALTER TABLE embedding_spaces ADD COLUMN basis_digest text")
    op.execute(
        "ALTER TABLE embedding_spaces ADD CONSTRAINT ck_embedding_spaces_projection_complete "
        "CHECK ((source_dimension IS NULL) = (basis_digest IS NULL))"
    )
    op.execute(
        "ALTER TABLE embedding_spaces ADD CONSTRAINT ck_embedding_spaces_projection_reduces "
        "CHECK (source_dimension IS NULL OR source_dimension > dimension)"
    )

    # One row per output dimension. Rows rather than an array column because the projection
    # is 512 inner products and pgvector's operator is what makes each one fast; an array of
    # arrays would be a matrix nothing in Postgres can multiply.
    op.execute(
        f"""
        CREATE TABLE embedding_space_axes (
            model text NOT NULL,
            version text NOT NULL,
            component integer NOT NULL,
            axis vector({SOURCE_DIMENSION}) NOT NULL,
            CONSTRAINT pk_embedding_space_axes PRIMARY KEY (model, version, component),
            CONSTRAINT ck_embedding_space_axes_component_non_negative CHECK (component >= 0),
            CONSTRAINT fk_embedding_space_axes_space
                FOREIGN KEY (model, version) REFERENCES embedding_spaces (model, version)
                ON DELETE CASCADE
        )
        """
    )
    # `zenith_app` reads the basis on every search and never writes it. A basis is changed by
    # `zenith fit-basis`, which runs on `owner_session` -- the factory CLAUDE.md already
    # names for CLI and provisioning.
    op.execute("GRANT SELECT ON embedding_space_axes TO zenith_app")
    op.execute("GRANT SELECT ON embedding_space_axes TO zenith_platform")

    op.execute(_PROJECT)


def _widen_the_vector_columns() -> None:
    """The typmod comes off, and a vector's width becomes its space's business.

    This is what lets a 512-dimensional space and a 1024-dimensional one hold rows in the
    same table at the same time -- and `pk_chunk_embeddings` already carries `(embedding_
    model, embedding_version)`, so one chunk holds one row per space with no schema change at
    all. That is the reindex path: build the new space beside the old one, flip which is
    active, and delete the retired rows when the operator is satisfied rather than when the
    migration says so.

    **The width is not lost, it moves.** `embedding_spaces.dimension` has always declared it
    and nothing read it; now the partial indexes below and the cast in `search.dense` both
    do, and a row whose width disagrees with its space cannot be compared to anything.

    The generated column is dropped and re-added rather than retyped, because `ALTER COLUMN
    TYPE` on a generated column is not accepted -- and because a generated column is
    recomputed from its expression on the rewrite either way, so nothing is being preserved
    that the re-add would not produce. Migration 0025's reason for keeping `embedding` as
    fp32 beside it is untouched and gets paid a second time here: the projection reads the
    fp32 vector, so reprojecting a corpus needs no embedding model and no TEI at all.
    """
    op.execute("DROP INDEX ix_chunk_embeddings_hnsw_half")
    op.execute("ALTER TABLE chunk_embeddings DROP COLUMN embedding_half")
    op.execute("ALTER TABLE chunk_embeddings ALTER COLUMN embedding TYPE vector")
    op.execute(
        "ALTER TABLE chunk_embeddings ADD COLUMN embedding_half halfvec "
        "GENERATED ALWAYS AS (embedding::halfvec) STORED"
    )


def _index_each_space_separately() -> None:
    """One partial expression index per space, and the predicate is the space filter.

    An operator class covers one width, so there is no single index over a column holding
    two. Making the index partial is not a workaround for that -- it is the accurate
    statement: an HNSW graph is a graph *of one space*, and two spaces were never going to
    share one.

    The predicate is exactly the `WHERE` clause `search.dense` already emits for the space,
    so naming a space is what selects its index. There is nothing extra for a query to
    remember.

    **Read the plan, never the timing.** A query whose cast does not match the index
    expression returns precisely the right rows from a sequential scan, silently, and no test
    in this repository can see it on a seeded corpus -- the failure migration 0025 shipped and
    `test_vector_index.py` exists to catch. `eval/svd-basis.sql` records
    `Index Scan using probe_v2` and `probe_v1` for the two-space case on a scratch database,
    and `test_vector_index.py` holds the real schema to the same standard.
    """
    op.execute(
        f"""
        CREATE INDEX ix_chunk_embeddings_hnsw_half ON chunk_embeddings
          USING hnsw ((embedding_half::halfvec({SOURCE_DIMENSION})) halfvec_cosine_ops)
          WITH (m = {M}, ef_construction = {EF_CONSTRUCTION})
          WHERE embedding_model = '{SHIPPED_MODEL}' AND embedding_version = '{SHIPPED_VERSION}'
        """
    )
    op.execute(_NAME_PARTITION_INDEXES)


#: 0026's rename, unchanged and copied rather than imported for the reason that file gives.
#:
#: A partition's index is named by Postgres from the partition and the column, so a plan
#: naming `chunk_embeddings_p017_expr_idx` says nothing about which declared index was
#: reached -- and "which index" is the entire subject of `test_vector_index.py`. Selected by
#: `pg_partition_root` rather than by a name pattern, because a pattern once matched a
#: scratch schema on the installation this was first run against.
_NAME_PARTITION_INDEXES = """
DO $do$
DECLARE entry record;
BEGIN
    FOR entry IN
        SELECT n.nspname AS schema,
               child.relname AS current_name,
               parent.relname || '_' || right(table_of.relname, 4) AS wanted
        FROM pg_inherits i
        JOIN pg_class child ON child.oid = i.inhrelid
        JOIN pg_class parent ON parent.oid = i.inhparent
        JOIN pg_index ix ON ix.indexrelid = child.oid
        JOIN pg_class table_of ON table_of.oid = ix.indrelid
        JOIN pg_namespace n ON n.oid = child.relnamespace
        WHERE table_of.relispartition
          AND pg_partition_root(table_of.oid) = 'public.chunk_embeddings'::regclass
          AND child.relname <> parent.relname || '_' || right(table_of.relname, 4)
    LOOP
        EXECUTE format('ALTER INDEX %I.%I RENAME TO %I',
                       entry.schema, entry.current_name, entry.wanted);
    END LOOP;
END $do$
"""


def downgrade() -> None:
    """Back to one space at 1,024 dimensions, and it refuses rather than truncates.

    A projected space cannot be represented by revision 0026's schema: its rows are 512
    numbers wide and the column it would go back into declares 1,024. There is no cast that
    recovers what a projection discarded, so the honest options are to refuse or to delete
    somebody's index, and this refuses.

    Reaching 0026 from a projected installation therefore means retiring the projected space
    first -- `zenith reproject --back` -- which is a decision an operator takes, in daylight,
    rather than a side effect of a rollback. The 1024 space is still there to go back to
    precisely because `upgrade` does not touch it.
    """
    op.execute(
        """
        DO $do$
        DECLARE projected int;
        BEGIN
            SELECT count(*) INTO projected FROM embedding_spaces WHERE source_dimension IS NOT NULL;
            IF projected > 0 THEN
                RAISE EXCEPTION
                    'downgrade would discard % projected embedding space(s)', projected
                USING HINT = 'A projected space stores fewer numbers than revision 0026 can '
                             'hold, and no cast recovers the rest. Retire the projected '
                             'space and delete its rows first, then run this again.';
            END IF;
        END $do$
        """
    )
    _require_lock_table()
    op.execute("SET LOCAL statement_timeout = 0")
    op.execute("SET LOCAL maintenance_work_mem = '512MB'")

    op.execute("DROP INDEX ix_chunk_embeddings_hnsw_half")
    op.execute("ALTER TABLE chunk_embeddings DROP COLUMN embedding_half")
    op.execute(
        f"ALTER TABLE chunk_embeddings ALTER COLUMN embedding TYPE vector({SOURCE_DIMENSION})"
    )
    op.execute(
        f"ALTER TABLE chunk_embeddings ADD COLUMN embedding_half halfvec({SOURCE_DIMENSION}) "
        f"GENERATED ALWAYS AS (embedding::halfvec({SOURCE_DIMENSION})) STORED"
    )
    op.execute(
        f"""
        CREATE INDEX ix_chunk_embeddings_hnsw_half ON chunk_embeddings
          USING hnsw (embedding_half halfvec_cosine_ops)
          WITH (m = {M}, ef_construction = {EF_CONSTRUCTION})
        """
    )
    op.execute(_NAME_PARTITION_INDEXES)

    op.execute("DROP FUNCTION zenith_project(vector, text, text)")
    op.execute("DROP TABLE embedding_space_axes")
    op.execute(
        "ALTER TABLE embedding_spaces DROP CONSTRAINT ck_embedding_spaces_projection_reduces"
    )
    op.execute(
        "ALTER TABLE embedding_spaces DROP CONSTRAINT ck_embedding_spaces_projection_complete"
    )
    op.execute("ALTER TABLE embedding_spaces DROP COLUMN basis_digest")
    op.execute("ALTER TABLE embedding_spaces DROP COLUMN source_dimension")
