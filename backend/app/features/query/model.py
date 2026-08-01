from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, created_at, uuid_col, uuid_pk


class Query(Base):
    """Query log: observability today, audit trail in iteration 4."""

    __tablename__ = "queries"

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid_col] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    user_id: Mapped[uuid_col | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    question: Mapped[str]
    answer: Mapped[str | None]
    model_used: Mapped[str | None]
    latency_retrieval_ms: Mapped[int | None]
    latency_generation_ms: Mapped[int | None]
    created_at: Mapped[created_at]


class QueryCitation(Base):
    """Scores from all four phases.

    This is what will make it possible, six months from now, to answer why one
    specific query returned garbage.
    """

    __tablename__ = "query_citations"

    query_id: Mapped[uuid_col] = mapped_column(
        ForeignKey("queries.id", ondelete="CASCADE"), primary_key=True
    )
    chunk_id: Mapped[uuid_col] = mapped_column(
        ForeignKey("chunks.id", ondelete="CASCADE"), primary_key=True
    )
    rank: Mapped[int]
    score_bm25: Mapped[float | None]
    score_vector: Mapped[float | None]
    score_rrf: Mapped[float | None]
    score_rerank: Mapped[float | None]
