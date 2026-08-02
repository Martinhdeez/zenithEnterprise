"""Bytes to chunks to vectors, and the refusals along the way.

The pipeline is one function with a status machine around it:

    pending → parsing → chunking → embedding → ready
                  └──────────────────────────→ failed

**`status='ready'` with zero chunks is forbidden.** Without that rule an image-only PDF
ingests *successfully*: no exception, no failed status, nothing in any log. The document
appears in the list, someone asks about it, and the system answers from a different
document entirely. There is no symptom until a customer stops trusting the answers, which
is the same failure shape as a silent leak and gets the same treatment — the system refuses
rather than proceeds. `eval/fixtures.py` builds the PDF that proves it.

**Re-running must not duplicate anything.** Procrastinate retries, and a document that
gained a second copy of every chunk on retry would double its weight in every later search
result. Pages and chunks for the document are deleted inside the same transaction that
writes the new ones.
"""

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.core.hardware import Profile
from app.core.hardware import active as active_profile
from app.features.documents.model import Chunk as ChunkRow
from app.features.documents.model import Document, Page
from app.features.documents.storage import DocumentStorage
from app.features.embeddings.client import DIMENSION, MODEL, VERSION, TeiClient
from app.features.embeddings.model import ChunkEmbedding, EmbeddingSpace
from app.features.ingestion.chunking.chunker import Chunk, chunk_page
from app.features.ingestion.parsers.base import ParsedPage
from app.features.ingestion.parsers.pdfplumber_parser import PdfPlumberParser
from app.features.ingestion.routing import Route, decide
from app.features.tenancy.context import TenantContext

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class Result:
    document_id: UUID
    status: str
    pages: int
    chunks: int
    detail: str | None = None


class IngestionPipeline:
    """One document, start to finish.

    The context comes from the caller rather than being resolved here, and that is the
    decision recorded in the F5 plan: writing `chunks` needs a context whose labels
    intersect the document's, but reading the document to *learn* its labels needs those
    labels already. The task payload carries them, captured when the upload wrote them, so
    the worker needs no bypass and the task stays a pure function of its payload.
    """

    def __init__(
        self,
        context: TenantContext,
        storage: DocumentStorage | None = None,
        embedder: TeiClient | None = None,
        profile: Profile | None = None,
    ) -> None:
        self.context = context
        self.profile = profile or active_profile()
        self.storage = storage or DocumentStorage()
        self.embedder = embedder or TeiClient(profile=self.profile)

    async def run(self, document_id: UUID) -> Result:
        async with tenant_session(self.context) as session:
            document = await session.get(Document, document_id)
            if document is None:
                # Invisible to this context. Either it was deleted, or it was relabelled
                # after the task was enqueued and the payload's labels are stale. Both are
                # ordinary; neither is something to retry blindly.
                return Result(document_id, "unknown", 0, 0, "document not visible in this context")
            path = self.storage.path_for(self.context.tenant_id, document.sha256)

        try:
            return await self._ingest(document_id, path)
        except Exception as exc:  # noqa: BLE001 - the failure has to reach the status column
            log.exception("ingestion_failed", document_id=str(document_id))
            await self._mark_failed(document_id, f"{type(exc).__name__}: {exc}")
            return Result(document_id, "failed", 0, 0, str(exc))

    async def _ingest(self, document_id: UUID, path: Path) -> Result:
        if not path.exists():
            await self._mark_failed(document_id, "the stored file is missing")
            return Result(document_id, "failed", 0, 0, "the stored file is missing")

        await self._set_status(document_id, "parsing")
        pages = PdfPlumberParser().parse(path)
        routed, unreadable = self._route(pages)

        await self._set_status(document_id, "chunking")
        chunks: list[Chunk] = []
        for page in routed:
            chunks.extend(chunk_page(page))

        if not chunks:
            # The rule this pipeline exists to enforce. `unreadable` says *why*, which is
            # the difference between an operator enabling OCR and an operator guessing.
            detail = (
                unreadable
                or "no text could be extracted from this document, so it has no searchable content"
            )
            await self._mark_failed(document_id, detail)
            return Result(document_id, "failed", len(pages), 0, detail)

        await self._set_status(document_id, "embedding")
        vectors = await self.embedder.embed([chunk.text for chunk in chunks])

        await self._persist(document_id, routed, chunks, vectors)
        return Result(document_id, "ready", len(pages), len(chunks))

    def _route(self, pages: list[ParsedPage]) -> tuple[list[ParsedPage], str | None]:
        """Apply the per-page decision and record what it saw.

        Docling is not wired yet — M0 moved the evidence for it to F9, since recall finds
        the page and cannot tell whether a model can *read* the table. Pages routed to
        `LAYOUT` are therefore parsed by pdfplumber for now and carry a warning saying so,
        rather than being dropped: a two-column page read badly is still better than a
        two-column page absent, and the warning is what makes the gap findable later.
        """
        routed: list[ParsedPage] = []
        unreadable_reason: str | None = None

        for page in pages:
            decision = decide(page, ocr_available=self.profile.ocr)
            if decision.route is Route.UNREADABLE:
                unreadable_reason = unreadable_reason or decision.reason
                continue
            warnings = decision.warnings
            if decision.route is Route.LAYOUT:
                warnings = (*warnings, f"{decision.reason}; layout parsing arrives in F9")
            routed.append(
                ParsedPage(
                    page_num=page.page_num,
                    text=page.text,
                    words=page.words,
                    method=page.method,
                    warnings=warnings,
                )
            )

        return routed, unreadable_reason

    async def _persist(
        self,
        document_id: UUID,
        pages: list[ParsedPage],
        chunks: list[Chunk],
        vectors: list[list[float]],
    ) -> None:
        """Everything in one transaction, including the status.

        A document reaching `ready` in a transaction that has not yet written its chunks is
        exactly the state the zero-chunk rule forbids, so the two cannot be separated.
        """
        async with tenant_session(self.context) as session:
            await _clear_previous(session, document_id)
            await _ensure_space(session)

            session.add_all(
                Page(
                    document_id=document_id,
                    page_num=page.page_num,
                    extraction_method=_method_of(page),
                    text=page.text,
                )
                for page in pages
            )

            rows = [
                ChunkRow(
                    document_id=document_id,
                    tenant_id=self.context.tenant_id,
                    page_num=chunk.page_num,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    bboxes=[box.as_dict() for box in chunk.boxes],
                    section=chunk.section,
                    text=chunk.text,
                )
                for chunk in chunks
            ]
            session.add_all(rows)
            # Flushed before the embeddings so the chunk ids exist. `label_ids` is filled
            # by the trigger from migration 0003 at INSERT time — a chunk born with an
            # empty array would be retrievable by the whole tenant.
            await session.flush()

            session.add_all(
                ChunkEmbedding(
                    chunk_id=row.id,
                    tenant_id=self.context.tenant_id,
                    embedding_model=MODEL,
                    embedding_version=VERSION,
                    embedding=vector,
                )
                for row, vector in zip(rows, vectors, strict=True)
            )

            document = await session.get(Document, document_id)
            if document is not None:
                document.page_count = len(pages)
                document.status = "ready"
                document.status_detail = _summarise(pages)

    async def _set_status(self, document_id: UUID, status: str) -> None:
        async with tenant_session(self.context) as session:
            document = await session.get(Document, document_id)
            if document is not None:
                document.status = status

    async def _mark_failed(self, document_id: UUID, detail: str) -> None:
        async with tenant_session(self.context) as session:
            document = await session.get(Document, document_id)
            if document is not None:
                document.status = "failed"
                # Truncated: this is read in a list column and by a person, and a stack
                # trace pasted into a status field helps nobody.
                document.status_detail = detail[:500]


async def _clear_previous(session: AsyncSession, document_id: UUID) -> None:
    """Idempotency. Chunk embeddings fall with the chunks through the cascade."""
    await session.execute(delete(ChunkRow).where(ChunkRow.document_id == document_id))
    await session.execute(delete(Page).where(Page.document_id == document_id))


async def _ensure_space(session: AsyncSession) -> None:
    """Register the vector space the first time anything is embedded.

    `embedding_spaces` is what makes hot reindexing possible (RNF-08): several spaces can
    exist at once, and the new one is built while the active one keeps serving. It carries
    no RLS because it describes models rather than content.
    """
    existing = await session.scalar(
        select(EmbeddingSpace).where(
            EmbeddingSpace.model == MODEL, EmbeddingSpace.version == VERSION
        )
    )
    if existing is None:
        session.add(
            EmbeddingSpace(model=MODEL, version=VERSION, dimension=DIMENSION, status="active")
        )
        await session.flush()


def _method_of(page: ParsedPage) -> str:
    """What actually read this page, plus any warning, in one column.

    The warning travels with the page rather than in a log because it is a property of the
    stored text: a page whose spacing is broken poisons the lexical index for as long as it
    is stored, and someone debugging a bad answer six months from now will be looking at
    this row, not at a log line that rotated away.
    """
    if not page.warnings:
        return page.method
    return f"{page.method} ({'; '.join(page.warnings)})"[:200]


def _summarise(pages: list[ParsedPage]) -> str | None:
    suspect = sum(1 for page in pages if page.warnings)
    if not suspect:
        return None
    return f"{suspect} of {len(pages)} page(s) extracted with warnings"
