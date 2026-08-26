"""Bytes to chunks to vectors, and the refusals along the way.

The pipeline is one function with a status machine around it:

    pending → parsing → chunking → embedding → classifying → ready
                  └────────────────────────────────────────→ failed

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
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import tenant_session
from app.core.hardware import Profile
from app.core.hardware import active as active_profile
from app.features.audit.service import record_automatic
from app.features.documents.media import is_paginated
from app.features.documents.model import Chunk as ChunkRow
from app.features.documents.model import Document, Page
from app.features.documents.storage import DocumentStorage
from app.features.embeddings.client import DIMENSION, MODEL, VERSION, TeiClient
from app.features.embeddings.model import ChunkEmbedding, EmbeddingSpace
from app.features.ingestion.chunking.chunker import Chunk, chunk_page, chunk_stream
from app.features.ingestion.classification import Classifier, Outcome
from app.features.ingestion.parsers.base import ParsedPage, Parser
from app.features.ingestion.parsers.pdfplumber_parser import PdfPlumberParser, page_count
from app.features.ingestion.parsers.text_parser import TextParser
from app.features.ingestion.routing import Route, decide
from app.features.labels.repository import LabelRepository
from app.features.tenancy.context import TenantContext

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class Routing:
    """What `_route` decided, including what it had to leave out."""

    pages: list[ParsedPage]
    #: Why the first omitted page could not be read. One reason, not one per page: they share
    #: a cause, and forty copies of the same sentence is not a better diagnosis.
    unreadable_reason: str | None
    #: Which pages produced nothing. Page numbers rather than a count, because "pages 12, 13
    #: and 14" tells somebody holding the PDF where to look and "3 pages" does not.
    omitted: tuple[int, ...] = ()


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
        classifier: Classifier | None = None,
    ) -> None:
        self.context = context
        self.profile = profile or active_profile()
        self.storage = storage or DocumentStorage()
        self.embedder = embedder or TeiClient(profile=self.profile)
        # Injected like the embedder rather than constructed inside `_file`: a test that
        # cannot choose the model reaching this step is a test of whatever the installation
        # default happens to answer, which for `MockProvider` is a sentence containing "[1]"
        # — a number, in range, that would file every document under its first label and
        # look like a passing test.
        self.classifier = classifier or Classifier(context)

    async def run(self, document_id: UUID) -> Result:
        async with tenant_session(self.context) as session:
            document = await session.get(Document, document_id)
            if document is None:
                # Invisible to this context. Either it was deleted, or it was relabelled
                # after the task was enqueued and the payload's labels are stale. Both are
                # ordinary; neither is something to retry blindly.
                return Result(document_id, "unknown", 0, 0, "document not visible in this context")
            path = self.storage.path_for(
                self.context.tenant_id, document.sha256, document.media_type
            )
            # Read inside the session: the instance detaches when it closes, and reading
            # an attribute afterwards raises rather than returning the value.
            media_type = document.media_type

        try:
            return await self._ingest(document_id, path, media_type)
        except Exception as exc:  # noqa: BLE001 - the failure has to reach the status column
            log.exception("ingestion_failed", document_id=str(document_id))
            await self._mark_failed(document_id, f"{type(exc).__name__}: {exc}")
            return Result(document_id, "failed", 0, 0, str(exc))

    async def _ingest(self, document_id: UUID, path: Path, media_type: str) -> Result:
        if not path.exists():
            await self._mark_failed(document_id, "the stored file is missing")
            return Result(document_id, "failed", 0, 0, "the stored file is missing")

        paginated = is_paginated(media_type)

        # Before parsing, because afterwards the cost has already been paid. `settings`
        # has carried `max_pages_per_document` since it was written and nothing read it —
        # a safeguard in name only, of the same kind as an abort listener nothing could
        # reach. A document past the limit is refused with the number in the message, so
        # the person holding a 4,000-page manual knows to split it rather than guessing.
        # A page limit is a page count, and a text file has none. The size limit that
        # already ran at the gate is what bounds it — `stash` enforces it while receiving,
        # so an oversized note never reaches here at all.
        pages_in_file = page_count(path) if paginated else 0
        if pages_in_file > settings.max_pages_per_document:
            detail = (
                f"this document has {pages_in_file} pages, more than the "
                f"{settings.max_pages_per_document}-page limit for a single file"
            )
            await self._mark_failed(document_id, detail)
            return Result(document_id, "failed", pages_in_file, 0, detail)

        await self._set_status(document_id, "parsing")
        # Annotated with the protocol so it is *checked* rather than described. `Parser`
        # sat in `parsers/base.py` documenting the contract every parser must meet and
        # nothing was ever declared to meet it, which is a contract in the same sense a
        # comment is.
        # The one branch on media type in this file, and it is here rather than spread
        # through the stages because the two paths differ in exactly two respects: which
        # parser reads the file, and whether the text is split per page or as one stream.
        # Everything after — embedding, persistence, classification, the "ready with zero
        # chunks is forbidden" rule — is identical, and a flag threaded through those
        # stages would invite them to diverge.
        parser: Parser = PdfPlumberParser() if paginated else TextParser(media_type)
        pages = parser.parse(path)

        # Page routing is a PDF question: it asks whether a page has an extractable text
        # layer or needs OCR. A text file has its text by definition, so it is not routed —
        # sending it through would make `decide` judge a whole document by the rule written
        # for one page, and refuse a short note as "no extractable text layer".
        routing = self._route(pages) if paginated else Routing(pages, None, ())

        await self._set_status(document_id, "chunking")
        chunks: list[Chunk] = []
        if paginated:
            for page in routing.pages:
                chunks.extend(chunk_page(page))
        else:
            chunks.extend(chunk_stream(routing.pages[0].text))

        if not chunks:
            # The rule this pipeline exists to enforce. The reason says *why*, which is the
            # difference between an operator acting and an operator guessing.
            detail = (
                routing.unreadable_reason
                or "no text could be extracted from this document, so it has no searchable content"
            )
            await self._mark_failed(document_id, detail)
            return Result(document_id, "failed", len(pages), 0, detail)

        await self._set_status(document_id, "embedding")
        vectors = await self.embedder.embed([chunk.text for chunk in chunks])

        await self._persist(document_id, routing.pages, chunks, vectors)

        # Before `_file`, so the two sentences arrive in the order they happened. This one is
        # about the document's *contents* and matters more than where it was filed: a search
        # over a document missing three pages answers confidently and incompletely, and this
        # is the only place that says so.
        if routing.omitted:
            await self._note(document_id, _omission(routing, len(pages)))

        # `_persist` leaves the document `classifying`, not `ready`, and this is where it
        # becomes ready. Migration 0017's uploader exception is `status <> 'ready'`, so
        # writing `ready` alongside the chunks switched it off for exactly the step it exists
        # to cover: a member who uploaded the file lost it from Documents while the model was
        # being asked where it belongs.
        #
        # Filing must never fail an ingestion — the chunks are committed and the document is
        # searchable whatever a classifier does — so a failure here still ends in `ready`.
        try:
            settled = await self._file(document_id, chunks)
        except Exception:  # noqa: BLE001
            log.exception("filing_failed", document_id=str(document_id))
            await self._note(
                document_id,
                "automatic filing could not run; this document is waiting for an "
                "administrator to choose its access labels",
            )
            settled = False

        # Only when filing left the labels alone. Swapping them takes the document out of this
        # worker's reach — the context carries the labels captured at upload, which is what
        # keeps the worker free of an RLS bypass — so `_file` writes the status inside the same
        # transaction as the swap, and says so. Writing it again from here would silently do
        # nothing and leave the document `classifying` for ever, which is how this was found.
        if not settled:
            await self._set_status(document_id, "ready")
        return Result(document_id, "ready", len(pages), len(chunks))

    async def _file(self, document_id: UUID, chunks: list[Chunk]) -> bool:
        """Ask a model where a document belongs when nobody said, and file it there.

        **Only a document carrying nothing but the tenant's quarantine label**, which is what
        "the uploader chose nothing" looks like since migration 0017. A document is never
        stored truly unlabelled — `DocumentService` refuses that, because an empty
        `label_ids` publishes it to the whole tenant — and it no longer waits in the *default*
        label either, because that one is granted to `member` as well as `admin` and so meant
        tenant-wide for the whole of ingestion. `DocumentService._resolve_labels` decides what
        lands in quarantine using the same rule this method uses to decide what it may touch;
        they are two ends of one condition and must not drift.

        **The quarantine label is replaced, not added to.** Labels are a union: holding any
        one of a document's labels opens it, so adding a label always widens. Leaving the
        quarantine label in place beside the chosen compartment would keep every
        administrator on the document forever. Swapping it is exactly the outcome the
        uploader would have got by ticking those labels by hand.

        **What happens when the model names nothing depends on why**, and
        `classification.Outcome` carries that. Declined or never asked releases the document
        into the tenant default, which is where an unfiled document went before quarantine
        existed. A model that was configured and broke leaves it quarantined: nobody has
        vouched for the document, and a timeout is not a reason to publish it.

        A document the uploader *did* label is never touched. Not out of deference to manual
        choice — because there is no rearrangement of somebody's deliberate compartments
        that a guess is allowed to make.

        **After `_persist`, not before.** Writing `document_labels` fires
        `zenith_sync_document_labels`, SECURITY DEFINER, which updates `documents.label_ids`
        and propagates onto the chunks that now exist. Doing it earlier would leave the
        chunk inserts failing their own policy, since this session's context still carries
        the labels the document had when the job was queued.
        """
        async with tenant_session(self.context) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT d.uploaded_by, d.label_ids, "
                        "  (SELECT id FROM access_labels WHERE is_default) AS default_id, "
                        "  (SELECT id FROM access_labels WHERE is_quarantine) AS quarantine_id "
                        "FROM documents d WHERE d.id = :d"
                    ),
                    {"d": document_id},
                )
            ).first()

        if row is None or row.quarantine_id is None:
            return False
        # Anything other than exactly the quarantine label means somebody chose — either the
        # uploader named compartments, or this document has already been filed. Neither is
        # this code's to rearrange.
        if list(row.label_ids) != [row.quarantine_id]:
            return False

        excerpt = "\n".join(chunk.text for chunk in chunks[:6])
        filing = await self.classifier.file(document_id, row.uploaded_by, excerpt)

        # Where the document goes when the model named nothing, and it depends entirely on
        # *why*. Quarantine only holds a document that nobody has vouched for; it is not a
        # place to leave documents because an installation has no model configured.
        if filing.outcome is Outcome.FAILED:
            # The one case that stays put. An administrator files it by hand, and the status
            # says so rather than leaving them to wonder why it is not in a folder.
            await self._note(
                document_id,
                "automatic filing failed; this document is waiting for an administrator "
                "to choose its access labels",
            )
            return False

        # Never the label it is already waiting in. `_candidates` no longer offers it, and
        # this is the second lock on the same door: applying it means inserting a row that
        # already exists, and the delete below then leaves `label_ids = '{}'` — the one value
        # that means *visible to the whole tenant*. A guess must not be able to reach that
        # state through any path, so the write refuses it as well as the offer.
        applied = [label for label in filing.labels if label != row.quarantine_id]
        if applied != filing.labels:
            log.warning("classifier_offered_reserved_label", document_id=str(document_id))

        if not applied:
            # Declined, or never asked. Both mean the document belongs where an unfiled
            # document went before quarantine existed: the tenant default.
            if row.default_id is None:
                # No default to release it into. Leaving it quarantined is the only option
                # that is not "publish it to the tenant", and it is the safe one.
                await self._note(
                    document_id,
                    "no default label exists to file this document into; "
                    "an administrator must choose its access labels",
                )
                return False
            applied = [row.default_id]
            if filing.outcome is Outcome.UNAVAILABLE:
                await self._note(
                    document_id,
                    "filed under the default label: no classification model is configured",
                )

        async with tenant_session(self.context) as session:
            for label_id in applied:
                await session.execute(
                    text(
                        "INSERT INTO document_labels (document_id, label_id) "
                        "VALUES (:d, :l) ON CONFLICT DO NOTHING"
                    ),
                    {"d": document_id, "l": label_id},
                )
            # Last, and only once the chosen labels are in place: dropping it first would
            # leave a window where the document carried no label at all, which is the one
            # state that means "visible to the whole tenant" rather than "visible to
            # nobody".
            # Ready, in the same transaction as the labels it is ready *with*, and written
            # **before** the quarantine label is dropped rather than after. The moment that
            # delete lands, the trigger rewrites `documents.label_ids` and the row stops
            # intersecting this worker's context — which carries the labels captured at
            # upload, and is what keeps the worker free of an RLS bypass. A write after it
            # finds nothing, and leaves the document `classifying` for ever. This is the only
            # point in the sequence where the document is both fully labelled and still
            # reachable by the session doing the work.
            document = await session.get(Document, document_id)
            if document is not None:
                document.status = "ready"
                # Flushed here rather than left to the commit, and this is not tidiness. An
                # ORM change is a *pending* UPDATE; without this it is written when the session
                # flushes, which is after the raw DELETE below — by which point the row no
                # longer intersects this context and the UPDATE matches zero rows. It failed
                # that way in one test and not another, purely on flush ordering, which is the
                # kind of dependency worth removing rather than understanding.
                await session.flush()

            await session.execute(
                text("DELETE FROM document_labels WHERE document_id = :d AND label_id = :l"),
                {"d": document_id, "l": row.quarantine_id},
            )

        # After the commit, and with no person as the actor. This is a change to who may read
        # a document, which is the one thing the trail exists for — and every *human* label
        # change was recorded while the automatic one was not, so the reach of the audit story
        # stopped exactly where automation began.
        # Names, for the reason `audit_events` denormalises `actor_email`: a row that loses
        # its subject when the subject changes records nothing, and this event's subject is a
        # set of labels — the things in this schema most likely to be renamed or merged.
        async with tenant_session(self.context) as session:
            names = await LabelRepository(session).names_of(applied)

        await record_automatic(
            self.context,
            "document.classified",
            target_type="document",
            target_id=document_id,
            labels=names,
            outcome=str(filing.outcome),
            uploaded_by=str(row.uploaded_by) if row.uploaded_by else None,
        )
        return True

    def _route(self, pages: list[ParsedPage]) -> "Routing":
        """Apply the per-page decision and record what it saw.

        Docling is not wired yet — M0 moved the evidence for it to F9, since recall finds
        the page and cannot tell whether a model can *read* the table. Pages routed to
        `LAYOUT` are therefore parsed by pdfplumber for now and carry a warning saying so,
        rather than being dropped: a two-column page read badly is still better than a
        two-column page absent, and the warning is what makes the gap findable later.

        **Dropped pages are counted, not merely reasoned about.** The reason alone was enough
        while the only case that mattered was a document where *every* page was unreadable —
        that ends in `failed`, and the reason is the whole message. A document where three
        pages of forty are scanned reached `ready` with those three missing from the index and
        nothing anywhere saying so, which is the same failure shape as a silent leak: no
        error, and answers drawn confidently from an incomplete document.
        """
        routed: list[ParsedPage] = []
        unreadable_reason: str | None = None
        omitted: list[int] = []

        for page in pages:
            decision = decide(page, ocr_available=self.profile.ocr_capable_hardware)
            if decision.route is Route.UNREADABLE:
                unreadable_reason = unreadable_reason or decision.reason
                omitted.append(page.page_num)
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

        return Routing(routed, unreadable_reason, tuple(omitted))

    async def _persist(
        self,
        document_id: UUID,
        pages: list[ParsedPage],
        chunks: list[Chunk],
        vectors: list[list[float]],
    ) -> None:
        """Everything in one transaction, including the status.

        A document reaching `ready` in a transaction that has not yet written its chunks is
        exactly the state the zero-chunk rule forbids. The rule is an ordering — `ready` never
        earlier than the chunks — so this writes `classifying` and `_ingest` writes `ready`
        once filing is done, which satisfies it and stops 0017's uploader exception being
        switched off during the step it was written for.
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
                # `classifying`, not `ready`. The zero-chunk rule this method exists to
                # enforce is about `ready` never *preceding* the chunks; writing it strictly
                # later than they are keeps that and closes migration 0017's window, whose
                # uploader exception is keyed on `status <> 'ready'`.
                document.status = "classifying"
                document.status_detail = _summarise(pages)

    async def _set_status(self, document_id: UUID, status: str) -> None:
        async with tenant_session(self.context) as session:
            document = await session.get(Document, document_id)
            if document is not None:
                document.status = status

    async def _note(self, document_id: UUID, detail: str) -> None:
        """Say something about a document without calling it failed.

        Filing is not ingestion: a document whose labels nobody chose is still parsed,
        chunked, embedded and searchable by those who reach it. Marking it `failed` would
        claim otherwise and invite somebody to re-ingest work that is already done. The
        status stays whatever it was; `status_detail` is where the sentence goes, because
        that is the column the documents list already shows.

        **Appends rather than replaces.** `_persist` has already written what the parse
        looked like — "3 of 40 page(s) extracted with warnings" — and a note that overwrote
        it would trade one true sentence for another. Both facts are about the same document
        and a reader needs them together: pages that came out badly, and where the document
        ended up filed. `_persist` writes the column fresh on every run, so a re-ingestion
        starts from one sentence rather than accumulating a history.
        """
        async with tenant_session(self.context) as session:
            document = await session.get(Document, document_id)
            if document is None:
                return
            existing = (document.status_detail or "").strip()
            combined = f"{existing}; {detail}" if existing else detail
            document.status_detail = combined[:500]

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


#: How many page numbers to name before the list stops being useful. A scanned appendix is
#: a run of pages, and "12, 13, 14 and 37 others" says everything "49 pages" does plus where
#: to start looking.
NAMED_PAGES = 8


def _omission(routing: "Routing", total: int) -> str:
    """The sentence a person needs when part of their document is not in the index.

    Deliberately not phrased as a warning about OCR. The reader is somebody who searched a
    contract and got a confident answer; what they need to know is that pages 12 to 14 were
    not part of what was searched, and only then why.
    """
    shown = ", ".join(str(number) for number in routing.omitted[:NAMED_PAGES])
    remaining = len(routing.omitted) - NAMED_PAGES
    listed = f"{shown} and {remaining} more" if remaining > 0 else shown
    reason = routing.unreadable_reason or "no text could be extracted"
    return f"{len(routing.omitted)} of {total} page(s) are not searchable (page {listed}): {reason}"


def _summarise(pages: list[ParsedPage]) -> str | None:
    suspect = sum(1 for page in pages if page.warnings)
    if not suspect:
        return None
    return f"{suspect} of {len(pages)} page(s) extracted with warnings"
