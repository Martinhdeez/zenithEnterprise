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

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

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
from app.features.auth.permissions import CATALOGUE
from app.features.auth.service import AccessProfile
from app.features.documents.model import DOCUMENT_STATUSES, Document
from app.features.documents.pagination import Cursor, clamp
from app.features.documents.repository import DocumentRepository
from app.features.documents.storage import DocumentStorage, Staged
from app.features.labels.repository import LabelRepository

PDF_MAGIC = b"%PDF-"

UPLOAD = "documents.upload"
DELETE_OWN = "documents.delete.own"
DELETE_ANY = "documents.delete.any"
assert {UPLOAD, DELETE_OWN, DELETE_ANY} <= set(CATALOGUE), (
    "the permissions this service is gated on must exist"
)


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


class DocumentService:
    def __init__(self, profile: AccessProfile, storage: DocumentStorage | None = None) -> None:
        self.profile = profile
        self.context = profile.context
        self.storage = storage or DocumentStorage()

    async def upload(
        self, filename: str, chunks: AsyncIterator[bytes], label_ids: list[UUID] | None = None
    ) -> Upload:
        staged = await self.storage.stash(_only_pdf(chunks))

        try:
            result = await self._record(filename, staged, label_ids)
        except BaseException:
            await self.storage.discard(staged)
            raise

        # After the transaction commits, never before. Inverted, a rolled-back transaction
        # would leave a file no row points at — an orphan nobody can find. This way the
        # worst case is a row whose file is missing, which is visible in the status column
        # and repairable by re-uploading.
        await self.storage.commit(staged, self.context.tenant_id)
        return result

    async def _record(self, filename: str, staged: Staged, requested: list[UUID] | None) -> Upload:
        async with tenant_session(self.context) as session:
            documents = DocumentRepository(session)
            labels = LabelRepository(session)
            wanted = await self._resolve_labels(labels, requested)

            existing = await documents.by_sha256(staged.sha256)
            if existing is not None:
                return await self._merge(documents, labels, existing, wanted)

            if await documents.count() >= settings.max_documents_per_tenant:
                raise LimitExceededError(
                    f"this tenant already holds {settings.max_documents_per_tenant} documents"
                )

            document = Document(
                tenant_id=self.context.tenant_id,
                filename=filename,
                sha256=staged.sha256,
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
        """
        current = await documents.label_ids_of(existing.id)
        union = current | wanted
        if union != current:
            await labels.set_document_labels(existing.id, sorted(union))
        return Upload(document=existing, labels=sorted(union), deduplicated=True)

    async def _resolve_labels(
        self, labels: LabelRepository, requested: list[UUID] | None
    ) -> set[UUID]:
        """Which labels this upload gets, and the refusal when there are none.

        A caller may only file a document under labels they themselves reach. Otherwise
        someone could write into a compartment they are locked out of — placing a document
        where they cannot see it, and cannot be held to have seen it.
        """
        if requested:
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

        default = await labels.default()
        if default is None:
            # F3 guarantees one per tenant at provisioning, so reaching this means the
            # installation was altered. Refusing is the only safe answer: storing the
            # document unlabelled would publish it to the whole tenant.
            raise ConflictError(
                "this tenant has no default access label, so an upload with no label "
                "specified cannot be classified. Set one before uploading."
            )
        return {default.id}

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
            await documents.delete(document)

        await self.storage.delete(self.context.tenant_id, sha256)

    async def page(
        self, limit: int | None = None, cursor: str | None = None, status: str | None = None
    ) -> tuple[list[Document], str | None]:
        """One page of documents, newest first.

        There is no unpaginated variant, deliberately. A tenant may hold five thousand
        documents, and a method returning all of them would eventually be called by
        something that only wanted the first twenty.
        """
        if status is not None and status not in DOCUMENT_STATUSES:
            raise NotFoundError(f"no such status: {status!r}")
        async with tenant_session(self.context) as session:
            documents, next_cursor = await DocumentRepository(session).page(
                limit=clamp(limit),
                cursor=Cursor.decode(cursor) if cursor else None,
                status=status,
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


async def _only_pdf(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Reject on the first bytes, before the rest of the upload is written.

    The declared content type is a claim the client makes, and F5's routing assumes what
    it is handed is really a PDF. Checking the magic number after the file has landed
    would work too, and would also mean writing 100 MB of somebody's video to disk before
    saying no.
    """
    head = b""
    async for chunk in chunks:
        if len(head) < len(PDF_MAGIC):
            head += chunk[: len(PDF_MAGIC) - len(head)]
            if len(head) >= len(PDF_MAGIC) and not head.startswith(PDF_MAGIC):
                raise UnsupportedFileError("only PDF files can be ingested")
        yield chunk

    if not head.startswith(PDF_MAGIC):
        raise UnsupportedFileError("only PDF files can be ingested")
