from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class DocumentResponse(BaseModel):
    id: UUID
    filename: str
    sha256: str
    status: str
    status_detail: str | None
    page_count: int | None
    size_bytes: int
    uploaded_by: UUID | None
    created_at: datetime

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
