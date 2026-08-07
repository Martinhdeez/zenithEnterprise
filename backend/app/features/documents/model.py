from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, created_at, uuid_col, uuid_pk

DOCUMENT_STATUSES = ("pending", "parsing", "chunking", "embedding", "ready", "failed")

#: The statuses that mean "still being worked on". Derived from the tuple above rather than
#: listed again, because F16 shipped a folder count filtering on a status named
#: `processing` that has never existed — it matched nothing, silently, and the constraint
#: only caught it because a test happened to insert one.
IN_FLIGHT = tuple(status for status in DOCUMENT_STATUSES if status not in {"ready", "failed"})


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        # Deduplication RF-03.3: the same file uploaded twice is not reprocessed.
        UniqueConstraint("tenant_id", "sha256"),
        CheckConstraint("status IN " + str(DOCUMENT_STATUSES), name="status_valido"),
        Index("ix_documents_label_ids", "label_ids", postgresql_using="gin"),
        # The listing order, so keyset pagination walks the index from the cursor instead
        # of sorting the tenant's whole corpus to return twenty rows. `id` is in it
        # because `created_at` is not unique: two documents inserted in one transaction
        # share a timestamp, and a cursor that cannot separate them skips or repeats one.
        Index("ix_documents_listing", "tenant_id", text("created_at DESC"), text("id DESC")),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid_col] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    filename: Mapped[str]
    sha256: Mapped[str]
    status: Mapped[str] = mapped_column(default="pending", server_default="pending")
    # Human-readable reason for the current status; on `failed`, the taxonomy error.
    status_detail: Mapped[str | None]
    # User-facing context a filename can't carry. Never read by ingestion or search —
    # display only. `Text` rather than an inferred `VARCHAR`, matching migration 0006:
    # `test_schema_matches_models` compares the two and a bare `Mapped[str | None]` maps
    # to `String()`, which is the drift it exists to catch.
    description: Mapped[str | None] = mapped_column(Text)
    # Copy of `document_labels`. Denormalised so the RLS policy on `documents` does
    # not have to read `document_labels`: if it did, and `document_labels` in turn
    # reads `documents`, the policies call each other and Postgres aborts on
    # mutual recursion.
    label_ids: Mapped[list[Any]] = mapped_column(ARRAY(PgUUID(as_uuid=True)), server_default="{}")
    page_count: Mapped[int | None]
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    uploaded_by: Mapped[uuid_col | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[created_at]


class Page(Base):
    """Text extracted per page.

    Persisting this stage is what makes reindexing cost hours instead of days: stored
    text is re-embedded without running the parser again.
    """

    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("document_id", "page_num"),)

    id: Mapped[uuid_pk]
    document_id: Mapped[uuid_col] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    page_num: Mapped[int]
    # pdfplumber | docling - routed per page, not per document.
    extraction_method: Mapped[str]
    text: Mapped[str]


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index("ix_chunks_label_ids", "label_ids", postgresql_using="gin"),
        Index("ix_chunks_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid_pk]
    document_id: Mapped[uuid_col] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    # Denormalised on purpose: the tenant filter must apply inside the vector query,
    # and a JOIN there penalises the HNSW index.
    tenant_id: Mapped[uuid_col] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    # Copy of the document's labels, for the same reason as `tenant_id`.
    label_ids: Mapped[list[Any]] = mapped_column(ARRAY(PgUUID(as_uuid=True)), server_default="{}")
    page_num: Mapped[int]
    # Text operations only. NOT for positioning the highlight.
    char_start: Mapped[int]
    char_end: Mapped[int]
    # [{page, x0, y0, x1, y1}, ...] normalised 0-1. A chunk spans several lines and
    # can cross pages, which is why it is a list.
    bboxes: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    section: Mapped[str | None]
    text: Mapped[str]
    # 1-2 sentences placing the chunk in context; prepended before embedding.
    context_prefix: Mapped[str | None]
    chunking_version: Mapped[int] = mapped_column(default=1, server_default="1")
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', text)", persisted=True)
    )
