"""Upload, deduplication and deletion.

Two rules run through everything here.

**A document is never stored without a label.** An empty `label_ids` array satisfies the
policy clause `label_ids = '{}' OR label_ids && zenith_current_labels()`, which means
visible to the entire tenant. That is deliberate for a document nobody has classified —
denying by default makes the product look broken — but it must never be how a document
*arrives*, because nothing in any log would say so.

**The database settles the races, not a check before the write.** Two simultaneous uploads
of the same bytes both pass a `SELECT`, and only the unique constraint knows which one won.
"""

import codecs
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.common.exceptions import (
    ConflictError,
    LimitExceededError,
    NotFoundError,
    PermissionDeniedError,
    UnsupportedFileError,
)
from app.core.config import settings
from app.core.database import tenant_session
from app.features.auth.access.permissions import CATALOGUE
from app.features.auth.service import AccessProfile
from app.features.documents.media import BY_SUFFIX, PDF, PDF_MAGIC
from app.features.documents.model import DOCUMENT_STATUSES, Document
from app.features.documents.pagination import Cursor, clamp
from app.features.documents.repository import DocumentRepository
from app.features.documents.schemas import DocumentInsights
from app.features.documents.storage import DocumentStorage, Staged
from app.features.ingestion.enqueue import enqueue_ingestion
from app.features.labels.model import AccessLabel
from app.features.labels.repository import LabelRepository

UPLOAD = "documents.upload"
DELETE_OWN = "documents.delete.own"
DELETE_ANY = "documents.delete.any"
assert {UPLOAD, DELETE_OWN, DELETE_ANY} <= set(CATALOGUE), (
    "the permissions this service is gated on must exist"
)


log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class Upload:
    """What the caller is told, including when nothing new was stored.

    `deduplicated` and `labels` are both in the response on purpose. The uploader has to
    be able to see that their file joined an existing document and which labels it now
    carries: widening access is acceptable when it is the visible consequence of an action
    someone took, and unacceptable when it is silent.
    """

    document: Document
    labels: list[UUID]
    deduplicated: bool


#: One sentence for the common refusal, so the message an uploader sees does not depend on
#: which branch of the gate rejected them.
UNSUPPORTED = "only PDF, plain text and Markdown files can be ingested"


class DocumentService:
    def __init__(self, profile: AccessProfile, storage: DocumentStorage | None = None) -> None:
        self.profile = profile
        # **Bound with the caller's id**, which `AccessProfile.context` deliberately leaves
        # unset — most policies decide everything from the tenant and the labels, so binding
        # it everywhere would be noise. Migration 0017 gives `documents` one clause that does
        # consult it: an uploader may read their own document while it is still ingesting,
        # which is what keeps a quarantined upload visible to the person who sent it.
        #
        # Bound once here rather than at each `tenant_session` call, for the reason
        # `TenantContext` gives about its own fields: a session that forgets it fails closed
        # and shows the uploader nothing, and that is precisely the kind of omission no test
        # notices unless it was looking for it.
        self.context = replace(profile.context, user_id=profile.user_id)
        self.storage = storage or DocumentStorage()

    async def upload(
        self,
        filename: str,
        chunks: AsyncIterator[bytes],
        label_ids: list[UUID] | None = None,
        description: str | None = None,
    ) -> Upload:
        media_type = intended_media_type(filename)
        staged = await self.storage.stash(_of_type(chunks, media_type))

        try:
            result = await self._record(filename, staged, label_ids, description, media_type)
        except BaseException:
            await self.storage.discard(staged)
            raise

        # After the transaction commits, never before. Inverted, a rolled-back transaction
        # would leave a file no row points at — an orphan nobody can find. This way the
        # worst case is a row whose file is missing, which is visible in the status column
        # and repairable by re-uploading.
        await self.storage.commit(staged, self.context.tenant_id, media_type)

        # Enqueued after the file is in place, so a worker that starts immediately finds
        # something to read. A deduplicated upload is not re-ingested: the bytes are
        # identical, and the only thing that changed is which labels reach them.
        if not result.deduplicated:
            await enqueue_ingestion(
                tenant_id=self.context.tenant_id,
                document_id=result.document.id,
                label_ids=result.labels,
            )
        return result

    async def _record(
        self,
        filename: str,
        staged: Staged,
        requested: list[UUID] | None,
        description: str | None,
        media_type: str,
    ) -> Upload:
        async with tenant_session(self.context) as session:
            documents = DocumentRepository(session)
            labels = LabelRepository(session)
            wanted = await self._resolve_labels(labels, requested)

            existing = await documents.by_sha256(staged.sha256)
            if existing is not None:
                # The name and description on this upload are dropped, not merged — same
                # rule as the bytes themselves: identical content is one document, and a
                # second uploader's title for it doesn't overwrite the first's. Only the
                # labels widen, because that's the one thing `_merge` already promises to
                # report back.
                return await self._merge(documents, labels, existing, wanted)

            if await documents.count() >= settings.max_documents_per_tenant:
                raise LimitExceededError(
                    f"this tenant already holds {settings.max_documents_per_tenant} documents"
                )

            document = Document(
                tenant_id=self.context.tenant_id,
                filename=filename,
                description=description,
                sha256=staged.sha256,
                media_type=media_type,
                size_bytes=staged.size_bytes,
                uploaded_by=self.profile.user_id,
            )
            session.add(document)
            try:
                await session.flush()
            except IntegrityError as exc:
                raise _duplicate_beyond_reach() from exc

            # Labels are written in the same transaction as the row. The trigger from
            # migration 0003 mirrors them into `documents.label_ids`, which is what RLS
            # reads, so the document is never committed in a visible-to-everyone state.
            await labels.set_document_labels(document.id, sorted(wanted))
            return Upload(document=document, labels=sorted(wanted), deduplicated=False)

    async def _merge(
        self,
        documents: DocumentRepository,
        labels: LabelRepository,
        existing: Document,
        wanted: set[UUID],
    ) -> Upload:
        """The same bytes, uploaded again by someone with a different label.

        The labels are unioned rather than the file being stored twice. The second uploader
        already holds the bytes, so nothing leaks towards them, and a second copy would
        cost the disk and — worse — put duplicate chunks into every search result.

        **Quarantine is exclusive**: a document either waits there alone or carries real
        labels, never both. Without that rule a union leaves it holding `{quarantine, …}`,
        which `_file` then declines to touch because those are not "exactly the quarantine
        label" — so the document keeps every administrator on it forever and is never
        released. Somebody naming a compartment for these bytes is a human classifying them,
        which is what quarantine was waiting for.
        """
        current = await documents.label_ids_of(existing.id)
        union = current | wanted
        quarantine = await labels.quarantine()
        if quarantine is not None and union > {quarantine.id}:
            union = union - {quarantine.id}
        if union != current:
            await labels.set_document_labels(existing.id, sorted(union))
        return Upload(document=existing, labels=sorted(union), deduplicated=True)

    async def _resolve_labels(
        self, labels: LabelRepository, requested: list[UUID] | None
    ) -> set[UUID]:
        """Which labels this upload gets, and where it waits when nobody chose one.

        A caller may only file a document under labels they themselves reach. Otherwise
        someone could write into a compartment they are locked out of — placing a document
        where they cannot see it, and cannot be held to have seen it.

        **"Nobody chose" includes naming exactly the tenant default.** The upload screen
        pre-ticks it, so the overwhelmingly common request is `[default]` and not an empty
        list, and treating those two differently would quarantine the API's uploads while
        leaving the product's own front door wide open. `IngestionPipeline._file` already
        draws the line in exactly this place to decide what the classifier may touch; the two
        ends have to agree, or a document gets protected and never reclassified, or
        reclassified having never been protected.
        """
        default = await labels.default()
        chose_nothing = not requested or (default is not None and set(requested) == {default.id})

        if requested and not chose_nothing:
            found = await labels.label_ids_in_tenant(requested)
            unknown = [str(label) for label in requested if label not in found]
            if unknown:
                raise NotFoundError(f"unknown label(s): {', '.join(unknown)}")
            beyond = [str(label) for label in requested if not self.context.reaches(label)]
            if beyond:
                raise PermissionDeniedError(
                    f"you cannot file a document under label(s) you do not hold: "
                    f"{', '.join(beyond)}"
                )
            return set(requested)

        # Quarantine, not the default label. The default is granted to `admin` *and*
        # `member`, so landing there means tenant-wide from the moment the upload answers
        # until the classifier runs at the end of ingestion — minutes, covering the document
        # list and the PDF download, not merely search. See migration 0017.
        quarantine = await labels.quarantine()
        if quarantine is None:
            from app.features.labels.provisioning import ensure_quarantine_label

            log.warning("quarantine_label_restored", tenant_id=str(self.context.tenant_id))
            quarantine = await ensure_quarantine_label(labels.session, self.context.tenant_id)
        return {quarantine.id}

    async def _default_label(self, labels: LabelRepository) -> AccessLabel:
        """The tenant's default, restored if somebody deleted it.

        Still needed with quarantine in place: the pipeline files a document here when the
        classifier declines or is not configured, which is the ordinary end state for an
        installation that has no model set up.
        """
        default = await labels.default()
        if default is None:
            # Provisioning guarantees one per tenant, so reaching this means somebody
            # deleted it — the label screen removes any label, that one included. Refusing
            # was the original answer and it is the wrong one: it locks the product's most
            # common action for every user of the tenant, and it says so in terms of a
            # concept nobody outside this codebase has heard of.
            #
            # Restoring is safe in the way refusing was trying to be. `ensure_default_label`
            # recreates exactly what provisioning would have — granted to the system roles
            # and to nothing else — so no document becomes readable by anybody who could not
            # have read an unclassified upload the day the tenant was made. What it is *not*
            # is a precedent for inventing labels elsewhere: the classifier still cannot,
            # and a label that carries meaning is somebody's decision, not a repair.
            from app.features.labels.provisioning import ensure_default_label

            log.warning("default_label_restored", tenant_id=str(self.context.tenant_id))
            default = await ensure_default_label(labels.session, self.context.tenant_id)
        return default

    async def delete(self, document_id: UUID) -> None:
        """Physical deletion, per RF-03.

        `pages`, `chunks`, `chunk_embeddings` and `query_citations` fall with the row
        through the cascades already in the schema. Deleting a document therefore erases
        the link between past answers and the passages that produced them — accepted
        deliberately: a right-to-erasure request outranks the immutability of an internal
        audit trail, and the audit design records the deletion event instead.

        Row first, file second, which is the opposite order to the upload and for the
        opposite reason. Unlinking first would let a failed transaction leave a row
        pointing at nothing; this way the worst case is an unreferenced file.
        """
        async with tenant_session(self.context) as session:
            documents = DocumentRepository(session)
            document = await documents.get(document_id)
            if document is None:
                # Also the answer when it exists but the caller's labels do not reach it.
                # "Forbidden" would confirm that it exists — see mvp.md 2.2.
                raise NotFoundError(f"no document {document_id}")
            if not self._may_delete(document):
                raise PermissionDeniedError("this document was uploaded by someone else")
            sha256 = document.sha256
            # Read inside the session with the digest: after `delete` the instance is
            # detached, and touching an attribute then raises rather than returning the
            # value — which would leave the file behind under a name we no longer know.
            media_type = document.media_type
            await documents.delete(document)

        await self.storage.delete(self.context.tenant_id, sha256, media_type)

    async def insights(self, document_id: UUID) -> DocumentInsights:
        """Passage count and how many answers have cited this document.

        Both aggregates run inside the caller's own tenant session, so RLS answers the
        access question before the arithmetic does: a document this caller cannot reach
        raises rather than returning zeros, because "no passages" and "not yours to see"
        are different statements and only one of them is about the document.
        """
        async with tenant_session(self.context) as session:
            exists = await session.scalar(
                text("SELECT 1 FROM documents WHERE id = :d"), {"d": document_id}
            )
            if not exists:
                raise NotFoundError("no such document")

            chunks = await session.scalar(
                text("SELECT count(*) FROM chunks WHERE document_id = :d"), {"d": document_id}
            )
            # The uploader by name, not by id. `documents.uploaded_by` is a foreign key and
            # the panel was rendering it raw — "uploaded by 7b1c5fd3-a760…" tells a reader
            # nothing at all, which is the same mistake the audit trail made with label ids.
            # LEFT JOIN, because the column is `ON DELETE SET NULL`: somebody can leave the
            # organisation and their uploads stay.
            uploader = await session.scalar(
                text(
                    "SELECT u.email FROM documents d LEFT JOIN users u ON u.id = d.uploaded_by "
                    "WHERE d.id = :d"
                ),
                {"d": document_id},
            )

            # Distinct queries, not citation rows: an answer that cited three passages from
            # the same document used it once, and counting rows would report three.
            answers = await session.scalar(
                text(
                    "SELECT count(DISTINCT c.query_id) FROM query_citations c "
                    # Composite since 0026: `chunks` is partitioned and `id` alone no longer
                    # identifies a row. Two columns rather than one, and the second is
                    # also what lets the planner prune every partition but the tenant's.
                    "JOIN chunks ch ON ch.id = c.chunk_id AND ch.tenant_id = c.tenant_id "
                    "WHERE ch.document_id = :d"
                ),
                {"d": document_id},
            )

        return DocumentInsights(
            chunks=int(chunks or 0), answers=int(answers or 0), uploaded_by=uploader
        )

    async def page(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        status: str | None = None,
        label_id: UUID | None = None,
        unlabelled: bool = False,
        search: str | None = None,
    ) -> tuple[list[Document], str | None]:
        """One page of documents, newest first.

        There is no unpaginated variant, deliberately. A tenant may hold five thousand
        documents, and a method returning all of them would eventually be called by
        something that only wanted the first twenty.

        `label_id` narrows to one folder — the counterpart to `documents/folders`'
        grouping, which only ever aggregated counts and never let a client ask for the rows
        behind one of them. `unlabelled` is the same narrowing for the one folder that has
        no id: `documents/folders` computes it from `label_ids = '{}'`, and this reads it
        the same way rather than inventing a second definition of "unlabelled" that could
        drift from the first.
        """
        if status is not None and status not in DOCUMENT_STATUSES:
            raise NotFoundError(f"no such status: {status!r}")
        async with tenant_session(self.context) as session:
            documents, next_cursor = await DocumentRepository(session).page(
                limit=clamp(limit),
                cursor=Cursor.decode(cursor) if cursor else None,
                status=status,
                label_id=label_id,
                unlabelled=unlabelled,
                search=search,
            )
        return documents, next_cursor.encode() if next_cursor else None

    async def get(self, document_id: UUID) -> Document:
        async with tenant_session(self.context) as session:
            document = await DocumentRepository(session).get(document_id)
        if document is None:
            raise NotFoundError(f"no document {document_id}")
        return document

    def _may_delete(self, document: Document) -> bool:
        if DELETE_ANY in self.profile.permissions:
            return True
        return document.uploaded_by == self.profile.user_id


def _duplicate_beyond_reach() -> ConflictError:
    """A document with these bytes exists, and the caller cannot see it.

    The unique constraint sees every row; RLS does not. So `by_sha256` can find nothing
    while the insert still collides — with a document held under labels this caller does
    not reach.

    Merging the labels here is what the deduplication rule implies, but it cannot be done
    from inside this transaction: the row is invisible, and reaching it would mean a fifth
    entry on the RLS bypass surface. That is a decision to take deliberately rather than
    inside an exception handler, so for now the upload is refused.

    The message says nothing about an existing document. It cannot: confirming that these
    exact bytes are already held would disclose the contents of a compartment the caller
    is locked out of.
    """
    return ConflictError(
        "this document cannot be stored under the labels requested. "
        "Ask an administrator to classify it."
    )


def intended_media_type(filename: str) -> str:
    """What the uploader is claiming this file is.

    From the name, and only the name, because a text file has no magic number to read. The
    claim is then *enforced* against the bytes by `_of_type` — this decides what to check,
    not what to believe.

    Anything unrecognised is treated as a claim of PDF, so a `.docx` is refused by the
    magic-number check with the message it has always given rather than by a second,
    differently-worded rejection.
    """
    return BY_SUFFIX.get(PurePosixPath(filename).suffix.lower(), PDF)


async def _of_type(chunks: AsyncIterator[bytes], media_type: str) -> AsyncIterator[bytes]:
    """Reject on the first bytes, before the rest of the upload is written.

    The declared content type is a claim the client makes, and routing assumes what it is
    handed is really what it says. Checking after the file has landed would work too, and
    would also mean writing 100 MB of somebody's video to disk before saying no.

    For a PDF that check is the magic number. For text there is none, so the check is that
    the bytes are decodable UTF-8 and hold no NUL — which is what actually distinguishes a
    document from a binary somebody renamed. Decoded incrementally: a multi-byte character
    split across two network chunks is normal, and a decoder that saw each chunk alone
    would reject perfectly good text with a plausible-looking error.
    """
    if media_type == PDF:
        head = b""
        async for chunk in chunks:
            if len(head) < len(PDF_MAGIC):
                head += chunk[: len(PDF_MAGIC) - len(head)]
                if len(head) >= len(PDF_MAGIC) and not head.startswith(PDF_MAGIC):
                    raise UnsupportedFileError(UNSUPPORTED)
            yield chunk

        if not head.startswith(PDF_MAGIC):
            raise UnsupportedFileError(UNSUPPORTED)
        return

    decoder = codecs.getincrementaldecoder("utf-8")()
    first = True
    async for chunk in chunks:
        if first and chunk.startswith(PDF_MAGIC):
            # A PDF under a `.txt` name. Refused rather than quietly stored as text: it
            # would ingest as mojibake and cite a character range into binary.
            raise UnsupportedFileError("this file is a PDF; upload it with a .pdf name")
        first = False
        try:
            decoded = decoder.decode(chunk)
        except UnicodeDecodeError:
            raise UnsupportedFileError("this file is not valid UTF-8 text") from None
        if "\x00" in decoded:
            raise UnsupportedFileError("this file contains binary data, not text")
        yield chunk

    try:
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        # Truncated multi-byte sequence at the very end.
        raise UnsupportedFileError("this file is not valid UTF-8 text") from None
