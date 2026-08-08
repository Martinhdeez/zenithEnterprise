from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, created_at, uuid_col, uuid_pk


class Query(Base):
    """Query log: observability today, audit trail in iteration 4."""

    __tablename__ = "queries"
    # The analytics dashboard's every statement is "this tenant, this window, newest first".
    __table_args__ = (Index("ix_queries_tenant_created", "tenant_id", text("created_at DESC")),)

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid_col] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    user_id: Mapped[uuid_col | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    question: Mapped[str]
    answer: Mapped[str | None]
    model_used: Mapped[str | None]
    latency_retrieval_ms: Mapped[int | None]
    latency_generation_ms: Mapped[int | None]
    #: What the provider said it spent, when it says anything. NULL means it reported
    #: nothing — a local binding does not, and a gateway may strip `usage` — which is a
    #: different answer from zero and is never summed as one. See `GenerationResponse`.
    prompt_tokens: Mapped[int | None]
    completion_tokens: Mapped[int | None]
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
