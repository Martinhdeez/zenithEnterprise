from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, created_at, uuid_col, uuid_pk
from app.features.documents.media import MEDIA_TYPES, PDF

#: `classifying` sits between `embedding` and `ready` because migration 0017's uploader
#: exception is keyed on `status <> 'ready'`, and filing happens after the chunks are
#: committed. Writing `ready` with them switched the exception off during the one step it was
#: written for. See migration 0019.
DOCUMENT_STATUSES = (
    "pending",
    "parsing",
    "chunking",
    "embedding",
    "classifying",
    "ready",
    "failed",
)

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
        CheckConstraint("media_type IN " + str(MEDIA_TYPES), name="media_type_conocido"),
        Index("ix_documents_label_ids", "label_ids", postgresql_using="gin"),
        # The listing order, so keyset pagination walks the index from the cursor instead
        # of sorting the tenant's whole corpus to return twenty rows. `id` is in it
        # because `created_at` is not unique: two documents inserted in one transaction
        # share a timestamp, and a cursor that cannot separate them skips or repeats one.
        Index("ix_documents_listing", "tenant_id", text("created_at DESC"), text("id DESC")),
        # Redundant with the primary key on `id`, and that is the point: a foreign key may
        # only target a unique constraint, and migration 0026 makes `fk_chunks_document_id`
        # composite so the cascade into a partitioned `chunks` carries a tenant to prune on.
        UniqueConstraint("id", "tenant_id", name="uq_documents_id_tenant_id"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid_col] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    filename: Mapped[str]
    sha256: Mapped[str]
    # Which viewer opens a citation into this document, and which parser read it. Stored
    # rather than inferred from the filename at render time: an extension is a guess that
    # is right until somebody uploads `notes.pdf.txt`.
    media_type: Mapped[str] = mapped_column(Text, default=PDF, server_default=PDF)
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
        # The BM25 index, migration 0022. The isolation columns are in it on purpose: a
        # predicate inside the Tantivy query is part of the search, while one outside it is
        # a filter over the search's output — and that destroys both the scoring and the
        # plan. See `zenith_lexical_search`.
        Index(
            "ix_chunks_bm25",
            "id",
            "text",
            "tenant_id",
            "label_ids",
            "unlabelled",
            postgresql_using="bm25",
            postgresql_with={
                "key_field": "'id'",
                "text_fields": (
                    '\'{"text": {"tokenizer": {"type": "en_stem", "lowercase": true}}}\''
                ),
            },
        ),
        Index("ix_chunks_label_ids", "label_ids", postgresql_using="gin"),
        Index("ix_chunks_tenant_id", "tenant_id"),
        # Composite since migration 0026, and unlike the keys *into* `chunks` this one was
        # not forced: `documents` is not partitioned, so `document_id` alone is still a
        # legal key. It carries the tenant so the cascade behind it does — `DELETE FROM
        # chunks WHERE document_id = $1` has no partition key and opens all 256 partitions
        # for writing, 1,552 locks for one deletion against 22.
        ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["documents.id", "documents.tenant_id"],
            name="fk_chunks_document_id",
            ondelete="CASCADE",
        ),
        # Migration 0026. Declared here as well as there so the drift test compares two
        # descriptions of the same table rather than one description and a blank.
        {"postgresql_partition_by": "HASH (tenant_id)"},
    )

    id: Mapped[uuid_pk]
    document_id: Mapped[uuid_col]
    # Denormalised on purpose: the tenant filter must apply inside the vector query,
    # and a JOIN there penalises the HNSW index.
    #
    # Part of the primary key since migration 0026, and not because a chunk needed a wider
    # identity: Postgres requires the partition key in every unique constraint on a
    # partitioned table. The consequence is that `id` alone no longer identifies a chunk,
    # which is why both foreign keys into this table are composite.
    tenant_id: Mapped[uuid_col] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    # Copy of the document's labels, for the same reason as `tenant_id`.
    label_ids: Mapped[list[Any]] = mapped_column(ARRAY(PgUUID(as_uuid=True)), server_default="{}")
    #: `None` for a document that has no pages — a `.txt` or `.md`. Not `1`: a column
    #: holding a placeholder teaches every reader that the value is always there, and the
    #: first one to render it writes "page 1" under a document without pages.
    page_num: Mapped[int | None]
    #: Where this chunk sits in **the stored text unit** — the page for a PDF, the whole
    #: file for a text document. Not document-relative: `chunk_page` restarts at zero on
    #: each page, so a reader assuming otherwise highlights the wrong span in every
    #: multi-page PDF, silently.
    #:
    #: Written since migration 0001 and read by nothing until text documents arrived. For
    #: a PDF the highlight is still the bounding boxes — pdfplumber's text and pdf.js's
    #: text layer disagree on whitespace, ligatures and hyphenation, so an offset computed
    #: against one misplaces the highlight in the other. A text file has exactly one text,
    #: and that objection does not apply to it.
    char_start: Mapped[int]
    char_end: Mapped[int]
    # [{page, x0, y0, x1, y1}, ...] normalised 0-1. A chunk spans several lines and
    # can cross pages, which is why it is a list.
    bboxes: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    #: `label_ids = '{}'`, materialised. Tantivy expresses "this field has no values"
    #: poorly, and the product's rule — a document with no labels is visible tenant-wide —
    #: has to be a first-class clause in the query rather than an absence.
    unlabelled: Mapped[bool] = mapped_column(
        Boolean, Computed("label_ids = '{}'::uuid[]", persisted=True)
    )
    section: Mapped[str | None]
    text: Mapped[str]
    # 1-2 sentences placing the chunk in context; prepended before embedding.
    context_prefix: Mapped[str | None]
    chunking_version: Mapped[int] = mapped_column(default=1, server_default="1")
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', text)", persisted=True)
    )
