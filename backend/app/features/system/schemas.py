from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class OrganisationResponse(BaseModel):
    id: UUID
    name: str
    #: `active` | `suspended` | `purging` | `purged`.
    status: str
    created_at: datetime
    status_changed_at: datetime | None
    users: int
    documents: int
    #: What the rows claim, not what the disk holds.
    storage_bytes: int


class ProvisionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    admin_email: EmailStr


class ProvisionResponse(BaseModel):
    organisation: OrganisationResponse
    admin_email: str
    #: **Returned exactly once.** Never stored in the clear, never logged, not recoverable.
    #: Same contract as `InviteResponse.password`, for the same reason: an on-premise
    #: install has no outbound mail, so the operator passes it on the way they already do.
    password: str


class PurgeRequest(BaseModel):
    """The organisation's name, typed again.

    Not the id. An id is copied and pasted without being read, and this is the last thing
    standing between a click and a customer's data. Verified server-side, so a wrong build
    of the front end cannot skip it.
    """

    confirm_name: str = Field(min_length=1)
