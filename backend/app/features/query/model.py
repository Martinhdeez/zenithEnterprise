from sqlalchemy import ForeignKey, ForeignKeyConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, created_at, uuid_col, uuid_pk


class Query(Base):
    """Query log: observability today, audit trail in iteration 4."""

    __tablename__ = "queries"
    # The analytics dashboard's every statement is "this tenant, this window, newest first".
    __table_args__ = (
        Index("ix_queries_tenant_created", "tenant_id", text("created_at DESC")),
        # Trigram, for the history search box. `ILIKE '%term%'` has no prefix to seek on, so
        # without this every search scans every question the tenant ever asked.
        Index(
            "ix_queries_question_trgm",
            "question",
            postgresql_using="gin",
            postgresql_ops={"question": "gin_trgm_ops"},
        ),
    )

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
    __table_args__ = (
        # Composite since migration 0026, because `chunks` is partitioned by `tenant_id` and
        # its primary key is `(id, tenant_id)`. The alternative on the table was to drop this
        # key rather than widen it, and 0026's docstring says why that was refused: the
        # `ON DELETE CASCADE` is what makes a citation disappear when its passage does.
        ForeignKeyConstraint(
            ["chunk_id", "tenant_id"],
            ["chunks.id", "chunks.tenant_id"],
            name="fk_query_citations_chunk_id",
            ondelete="CASCADE",
        ),
    )

    query_id: Mapped[uuid_col] = mapped_column(
        ForeignKey("queries.id", ondelete="CASCADE"), primary_key=True
    )
    chunk_id: Mapped[uuid_col] = mapped_column(primary_key=True)
    #: A foreign-key carrier, and deliberately not an access control.
    #:
    #: **This table's policy stays derived.** `EXISTS (SELECT 1 FROM queries q WHERE ...)`
    #: applies the policy on `queries`, which since migration 0005 is per *user* and not per
    #: tenant, because the questions people ask are more revealing than the documents they
    #: read. Rewriting it as `tenant_id = zenith_current_tenant()` now that the column exists
    #: would let anyone in the organisation see which passages a colleague's question pulled
    #: back — the substance of a question they may not read. That is the whole reason 0005
    #: left this table alone.
    tenant_id: Mapped[uuid_col] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    rank: Mapped[int]
    score_bm25: Mapped[float | None]
    score_vector: Mapped[float | None]
    score_rrf: Mapped[float | None]
    score_rerank: Mapped[float | None]
