from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, ForeignKey, Index
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
        # `vector_cosine_ops` because BGE-M3 returns normalised vectors.
        # Declared here, not only in the migration, so the drift test can check that
        # database and models say the same thing.
        Index(
            "ix_chunk_embeddings_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
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
