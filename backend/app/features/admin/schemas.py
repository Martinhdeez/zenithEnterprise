from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class RoleResponse(BaseModel):
    id: UUID
    name: str
    #: Seeded per tenant and not editable. A screen that offers to edit one is offering a
    #: 409.
    is_system: bool
    permissions: list[str]
    users: int
    #: Clearance, 1 to 10: the highest label level this role's holders reach through a
    #: group. Vertical only — it moves nobody into a group they are not in.
    priority_level: int
    #: Labels granted outright, ignoring both group and clearance. The exception route,
    #: recorded as its own rows so an access review can see somebody chose it.
    label_ids: list[UUID]


class RoleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100, default="")
    #: The complete set the role should hold afterwards, not a delta.
    permissions: list[str] = Field(default_factory=list[str])
    priority_level: int = Field(default=1, ge=1, le=10)


class RoleClearanceRequest(BaseModel):
    priority_level: int = Field(ge=1, le=10)


class RoleLabelsRequest(BaseModel):
    #: The complete set of outright grants afterwards, not a delta.
    label_ids: list[UUID] = Field(default_factory=list[UUID])


class AssignRolesRequest(BaseModel):
    #: The complete set of roles the user should hold afterwards. An empty list is valid
    #: and means "no roles" — a user who can log in and do nothing, which is a legitimate
    #: intermediate state while somebody is being moved between teams.
    role_ids: list[UUID] = Field(default_factory=list[UUID])


class MemberResponse(BaseModel):
    id: UUID
    email: str
    #: What they chose to be called. Null for anyone who never set one — every screen falls
    #: back to the address, which is the value that is always there.
    name: str | None
    role_ids: list[UUID]
    group_ids: list[UUID]


class LlmConfigResponse(BaseModel):
    endpoint_url: str
    model_name: str
    #: Never the key itself.
    has_api_key: bool
    #: False when this tenant has none and the installation default is being reported.
    configured: bool


class LlmConfigRequest(BaseModel):
    endpoint_url: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    #: Omit to keep the stored key; send "" to clear it.
    api_key: str | None = None


class InviteRequest(BaseModel):
    email: EmailStr
    #: Roles the new user starts with. Empty is valid — a user who can sign in and do
    #: nothing is a legitimate intermediate state while somebody is being onboarded.
    role_ids: list[UUID] = Field(default_factory=list[UUID])


class InviteResponse(BaseModel):
    user_id: UUID
    email: str
    #: **Returned exactly once.** Never stored in the clear, never logged, not recoverable.
    #: There is no email server on an on-premise install to send it, so the administrator
    #: passes it on the way they already pass on credentials.
    password: str
    role_ids: list[UUID]


class TotalsResponse(BaseModel):
    queries: int
    users: int
    prompt_tokens: int
    completion_tokens: int
    #: How many of those queries reported no usage at all. Shown rather than folded into the
    #: totals as zeros: a local model reports nothing, and an unknown cost is not a zero
    #: cost. See `GenerationResponse`.
    queries_without_usage: int
    average_retrieval_ms: int
    average_generation_ms: int
    abstentions: int


class ActiveUserResponse(BaseModel):
    user_id: UUID | None
    email: str | None
    queries: int


class CitedDocumentResponse(BaseModel):
    document_id: UUID
    filename: str
    answers: int


class AuditEntryResponse(BaseModel):
    query_id: UUID
    asked_at: datetime
    email: str | None
    question: str
    abstained: bool
    model: str | None
    latency_ms: int
    #: What the answer read. The part an auditor asks about, and not derivable from the
    #: answer text.
    documents: list[str]


class AnalyticsResponse(BaseModel):
    """The aggregates. The audit log is its own page — see `GET /analytics/audit`."""

    window_days: int
    totals: TotalsResponse
    most_active: list[ActiveUserResponse]
    top_cited: list[CitedDocumentResponse]


class AuditPageResponse(BaseModel):
    entries: list[AuditEntryResponse]
    #: Opaque. Pass it back as `cursor`; `null` means this is the last page.
    next_cursor: str | None
