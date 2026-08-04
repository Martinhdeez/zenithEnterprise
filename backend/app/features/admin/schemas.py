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


class RoleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100, default="")
    #: The complete set the role should hold afterwards, not a delta.
    permissions: list[str] = Field(default_factory=list[str])


class AssignRolesRequest(BaseModel):
    #: The complete set of roles the user should hold afterwards. An empty list is valid
    #: and means "no roles" — a user who can log in and do nothing, which is a legitimate
    #: intermediate state while somebody is being moved between teams.
    role_ids: list[UUID] = Field(default_factory=list[UUID])


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
