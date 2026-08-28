from pgvector.sqlalchemy import HALFVEC, Vector
from sqlalchemy import CheckConstraint, Computed, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, created_at, uuid_col

EMBEDDING_DIM = 1024  # BGE-M3
SPACE_STATUSES = ("building", "active", "retired")


class EmbeddingSpace(Base):
    """Registry of vector spaces (RNF-08).

    Vectors from two different models are not comparable. Keeping several spaces
    alive at once is what makes hot reindexing and rollback possible: the new one is
    built while the active one keeps serving queries.
    """

    __tablename__ = "embedding_spaces"
    __table_args__ = (CheckConstraint("status IN " + str(SPACE_STATUSES), name="status_valido"),)

    model: Mapped[str] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(primary_key=True)
    dimension: Mapped[int]
    status: Mapped[str]
    created_at: Mapped[created_at]


class ChunkEmbedding(Base):
    __tablename__ = "chunk_embeddings"
    __table_args__ = (
        # The index is on the fp16 representation, migration 0025. `halfvec_cosine_ops`
        # because BGE-M3 returns normalised vectors, and fp16 because the index is what has
        # to stay resident: 2,729.9 bytes per vector against 8,188.4, at index recall
        # 1.0000 against exact at both depths on the full corpus. Measured in
        # `eval/quantisation.json`. Declared here, not only in the migration, so the drift
        # test can check that database and models say the same thing.
        Index(
            "ix_chunk_embeddings_hnsw_half",
            "embedding_half",
            postgresql_using="hnsw",
            postgresql_ops={"embedding_half": "halfvec_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
    )

    chunk_id: Mapped[uuid_col] = mapped_column(
        ForeignKey("chunks.id", ondelete="CASCADE"), primary_key=True
    )
    # Denormalised so the RLS policy is an equality rather than an EXISTS against
    # `chunks`. This table sits on the hot path of vector search: a per-row subquery
    # here is paid on every single query.
    tenant_id: Mapped[uuid_col] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    embedding_model: Mapped[str] = mapped_column(primary_key=True)
    embedding_version: Mapped[str] = mapped_column(primary_key=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
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
        HALFVEC(EMBEDDING_DIM),
        Computed(f"embedding::halfvec({EMBEDDING_DIM})", persisted=True),
    )
