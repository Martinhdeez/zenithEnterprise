from pgvector.sqlalchemy import HALFVEC, Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, created_at, uuid_col

#: What BGE-M3 returns, and what a vector is before any space projects it.
#:
#: No longer the width of the stored column. Since migration 0027 a row is as wide as its
#: space says, and this is the *source* width — the input to a projection and the width of
#: every axis in `EmbeddingSpaceAxis`.
EMBEDDING_DIM = 1024  # BGE-M3
SPACE_STATUSES = ("building", "active", "retired")

#: The space this installation ships with: the identity, no projection, 1,024 wide.
#:
#: An HNSW index cannot be built over "whatever is active" — a partial index needs a literal
#: predicate — so the shipped space is the one place a space identity is written down instead
#: of read. It agrees with `embeddings.client.MODEL`/`VERSION` and with migration 0027.
SHIPPED_MODEL = "BAAI/bge-m3"
SHIPPED_VERSION = "1"


class EmbeddingSpace(Base):
    """Registry of vector spaces (RNF-08).

    Vectors from two different models are not comparable. Keeping several spaces
    alive at once is what makes hot reindexing and rollback possible: the new one is
    built while the active one keeps serving queries.

    Since migration 0027 a space may also carry a **projection**, and that is why the basis
    lives here rather than in a constant somewhere: `svd_512` is not a property of the model,
    it is a property of one space fitted on one corpus. Two installations running the same
    model with differently-fitted bases produce silently different neighbours, which is what
    `basis_digest` exists to make visible.
    """

    __tablename__ = "embedding_spaces"
    __table_args__ = (
        CheckConstraint("status IN " + str(SPACE_STATUSES), name="status_valido"),
        # A projection is described by both columns or by neither. Half a description is the
        # state that would let a space claim to project while `zenith_project` returned
        # nothing for it, and the query would then compare a full-width vector against
        # full-width rows and simply be the identity — a silent no-op rather than an error,
        # which is the one failure this stage does not otherwise have.
        CheckConstraint(
            "(source_dimension IS NULL) = (basis_digest IS NULL)",
            name="projection_complete",
        ),
        CheckConstraint(
            "source_dimension IS NULL OR source_dimension > dimension",
            name="projection_reduces",
        ),
    )

    model: Mapped[str] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(primary_key=True)
    #: The width of a stored vector in this space, and since 0027 the width the query vector
    #: is cast to. It has always been here and nothing read it; the partial index and
    #: `search.dense` both do now.
    dimension: Mapped[int]
    status: Mapped[str]
    created_at: Mapped[created_at]
    #: Non-null exactly when this space projects, and then it is the width of the input.
    #:
    #: A column rather than a join, because "does this space project" is asked on every
    #: single search and the answer decides whether the query vector goes through
    #: `zenith_project` at all.
    source_dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: sha256 over the axes as stored. Two bases fitted from different corpora are both
    #: valid, both orthonormal and produce different neighbours; this is the cheap way to
    #: ask two installations whether they are running the same fit.
    basis_digest: Mapped[str | None] = mapped_column(nullable=True)


class EmbeddingSpaceAxis(Base):
    """One output dimension of a space's projection. Migration 0027.

    **Uncentred SVD, never centred PCA.** These are the top `dimension` right singular
    vectors of the *uncentred* corpus matrix — the eigenbasis of `X'X / n`. Centring is not a
    rotation: it moves every point and changes every norm, and cosine similarity is a
    function of the norms. Measured in `eval/dimensions.json`, `pca_1024` is a rotation that
    discards *nothing* and still costs 18.7 points of index recall@10. Nothing that re-fits
    this table may subtract a mean.

    Rows rather than an array-of-arrays column, because the projection is `dimension` inner
    products and pgvector's operator is what makes each one fast. A matrix column would be a
    matrix nothing in Postgres can multiply.

    No RLS, for the reason `EmbeddingSpace` has none: it describes a model, not content.
    `zenith_app` is granted `SELECT` and nothing else — a basis is written by
    `zenith fit-basis`, through the CLI's owning connection.

    That last sentence names the factory obliquely on purpose. `tests/integration/
    test_unpruned_query_audit.py` flags any module that mentions a bypass factory *and* names
    a partitioned table in a string literal, and `__tablename__` here is one; the detector is
    coarse deliberately, because narrowing it to real call sites would need a SQL parser. A
    module that only talks about a factory is a false positive, and the fix for a false
    positive is not to add it to the allowlist — that is how an allowlist stops meaning
    anything.
    """

    __tablename__ = "embedding_space_axes"
    __table_args__ = (
        CheckConstraint("component >= 0", name="component_non_negative"),
        ForeignKeyConstraint(
            ["model", "version"],
            ["embedding_spaces.model", "embedding_spaces.version"],
            name="fk_embedding_space_axes_space",
            ondelete="CASCADE",
        ),
    )

    model: Mapped[str] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(primary_key=True)
    #: Zero-based, and the order is the order of the singular values. The basis is **nested**
    #: — the first `k` axes are exactly the basis that would have been fitted for `k` — which
    #: is a property of the eigendecomposition and is what lets one fit answer for every
    #: width. Anything that reads this table must order by it.
    component: Mapped[int] = mapped_column(primary_key=True)
    axis: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))


class ChunkEmbedding(Base):
    __tablename__ = "chunk_embeddings"
    __table_args__ = (
        # One partial expression index per space, and the predicate is exactly the space
        # filter `search.dense` already emits — so naming a space is what selects its index,
        # and a query has nothing extra to remember.
        #
        # Partial is not a workaround for an operator class covering one width. It is the
        # accurate statement: an HNSW graph is a graph *of one space*, and two spaces were
        # never going to share one. It is what lets the 1024 space keep serving while a 512
        # space is built beside it, which is the reindex path and the reason
        # `embedding_spaces` exists at all.
        #
        # `halfvec_cosine_ops` because BGE-M3 returns normalised vectors, and fp16 because
        # the index is what has to stay resident: 2,729.9 bytes per vector against 8,188.4,
        # at index recall 1.0000 against exact at both depths on the full corpus, measured in
        # `eval/quantisation.json`. At 512 dimensions it is 1,365.8, measured in
        # `eval/dimensions.json` — the 2.00x this stage is worth.
        #
        # Declared here and not only in the migration so the drift test can check that
        # database and models say the same thing.
        #
        # The operator class goes in `postgresql_ops` rather than inside the expression
        # text, and that is not style. With it inline, alembic's autogenerate warns
        # `Expression compare cannot proceed` and **skips the index entirely** — so
        # `test_schema_matches_models` would pass over the one object this stage changed
        # most, and the drift test would be silently blind exactly where it is needed.
        Index(
            "ix_chunk_embeddings_hnsw_half",
            text(f"(embedding_half::halfvec({EMBEDDING_DIM}))"),
            postgresql_using="hnsw",
            postgresql_ops={f"(embedding_half::halfvec({EMBEDDING_DIM}))": "halfvec_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_where=text(
                f"embedding_model = '{SHIPPED_MODEL}' AND embedding_version = '{SHIPPED_VERSION}'"
            ),
        ),
        # Composite since migration 0026: `chunks` is partitioned by `tenant_id`, so its
        # primary key is `(id, tenant_id)` and nothing can reference `chunks.id` alone. The
        # column was already here for the policy, so this costs no storage — and it buys a
        # guarantee that did not exist before, that an embedding cannot reference a chunk
        # belonging to a different tenant.
        ForeignKeyConstraint(
            ["chunk_id", "tenant_id"],
            ["chunks.id", "chunks.tenant_id"],
            name="fk_chunk_embeddings_chunk_id",
            ondelete="CASCADE",
        ),
        {"postgresql_partition_by": "HASH (tenant_id)"},
    )

    chunk_id: Mapped[uuid_col] = mapped_column(primary_key=True)
    # Denormalised so the RLS policy is an equality rather than an EXISTS against
    # `chunks`. This table sits on the hot path of vector search: a per-row subquery
    # here is paid on every single query.
    #
    # In the primary key since 0026, for the same reason as `Chunk.tenant_id`: the partition
    # key has to be in every unique constraint on a partitioned table.
    tenant_id: Mapped[uuid_col] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    # Already in the primary key before 0027, which is why one chunk can hold a row in the
    # old space and a row in the new one at the same time with no schema change at all. The
    # reindex path was designed into 0001 and this stage is the first thing to use it.
    embedding_model: Mapped[str] = mapped_column(primary_key=True)
    embedding_version: Mapped[str] = mapped_column(primary_key=True)
    #: Untyped width since migration 0027: a row is as wide as its space says it is.
    #:
    #: That is what makes mixing two bases impossible rather than merely discouraged. pgvector
    #: refuses to compare vectors of different widths — `different halfvec dimensions 1024 and
    #: 512` — so an unprojected query vector aimed at a projected space is an error at the
    #: first row touched, never a ranking. `eval/svd-basis.sql` demonstrates all three ways of
    #: getting it wrong and all three errors.
    #:
    #: Still fp32, and migration 0025's reason gets paid a second time here: the projection
    #: reads this column, so reprojecting a corpus needs no embedding model and no TEI.
    embedding: Mapped[list[float]] = mapped_column(Vector())
    #: The same vector at half the precision, and the one the HNSW index covers.
    #:
    #: Derived rather than stored in place: `ALTER COLUMN embedding TYPE halfvec` rounds,
    #: so a rollback would restore the type and not the values. Keeping fp32 also keeps
    #: exact rescoring possible, which is what any two-stage retrieval over a more
    #: aggressively compressed index would need. See migration 0025.
    #:
    #: Generated, so no write path can let it drift from `embedding` — and not writable,
    #: which is why the ingestion INSERT is unchanged.
    embedding_half: Mapped[list[float]] = mapped_column(
        HALFVEC(),
        Computed("embedding::halfvec", persisted=True),
    )
