from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class DocumentResponse(BaseModel):
    id: UUID
    filename: str
    description: str | None
    sha256: str
    status: str
    status_detail: str | None
    page_count: int | None
    size_bytes: int
    uploaded_by: UUID | None
    created_at: datetime
    #: The labels this document carries, read from `documents.label_ids` — the denormalised
    #: copy RLS itself evaluates, so what a client is shown is what the policy used.
    #:
    #: Ids rather than names: a name is a disclosure, and the caller may reach only some of
    #: these. The client resolves the ones it already holds from `GET /labels` and shows
    #: nothing for the rest, which keeps this endpoint from leaking a compartment's name
    #: through a document somebody can otherwise see.
    label_ids: list[UUID]

    model_config = {"from_attributes": True}


class DocumentPage(BaseModel):
    """A page of documents and where to resume.

    `next_cursor` is null on the last page, which is the only end-of-list signal: there is
    no total. Counting under RLS means evaluating the policy over every row in the tenant
    to produce a number that is stale by the time it is read, and no client behaviour here
    depends on knowing it.
    """

    items: list[DocumentResponse]
    next_cursor: str | None


class UploadResponse(BaseModel):
    """The document, plus what the upload actually did.

    `deduplicated` and `labels` are part of the contract rather than diagnostics. When an
    upload joins a document that already existed, the labels are unioned — so the caller
    has to be able to see both that nothing new was stored and which labels the document
    now carries. A widening of access is acceptable as the visible consequence of an
    action someone took; the same widening, unreported, is not.
    """

    document: DocumentResponse
    labels: list[UUID]
    deduplicated: bool


class FolderResponse(BaseModel):
    """One folder. `label_id` is null for the unlabelled bucket, which is not a label."""

    label_id: UUID | None
    name: str
    is_default: bool
    documents: int
    #: Broken out because a folder with three ready documents and one failed is a different
    #: thing from one with four, and a failed document is invisible in search.
    ready: int
    processing: int
    failed: int


class FolderTreeResponse(BaseModel):
    folders: list[FolderResponse]
    total_documents: int
