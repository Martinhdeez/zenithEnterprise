from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    # Bounded so a multi-megabyte "password" cannot turn one request into an argon2
    # denial of service. The lower bound is a floor, not a policy: password rules
    # belong with user management in M3.
    password: str = Field(min_length=8, max_length=1024)


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    # The same floor `LoginRequest` uses. A real policy belongs somewhere both this and
    # invitation can read it, and inventing one here would apply it to changes and not to
    # the generated passwords beside them.
    new_password: str = Field(min_length=8, max_length=1024)


class ProfileUpdate(BaseModel):
    """The parts of a profile its owner may change.

    Only the display name. Email is the login identifier and is unique per tenant, so
    changing it is an account operation with a uniqueness check and a re-authentication
    question attached, not a text field. Roles and labels are an administrator's to grant
    — a profile screen that let you widen your own access would defeat the point of having
    them.
    """

    #: Empty string clears it, which is why the floor is zero rather than one: somebody who
    #: filled this in by mistake needs a way back to "not set".
    name: str = Field(max_length=200)


class ProfileResponse(BaseModel):
    """Who the caller is, for the screen that shows it.

    `labels` is names rather than ids on purpose: it exists to answer "why can I not see
    the document my colleague can", and an id answers nothing. It is still not a
    permission check — RLS decides, in the database.
    """

    user_id: UUID
    email: str
    name: str | None
    tenant_id: UUID
    tenant_name: str | None
    roles: list[str]
    permissions: list[str]
    labels: list[str]
    documents_uploaded: int
    created_at: datetime


class MeResponse(BaseModel):
    user_id: UUID
    tenant_id: UUID
    permissions: list[str]
    # The labels this caller reaches. Returned so the UI can hide what the user cannot
    # open, never so it can decide: the decision is RLS's, in the database.
    label_ids: list[UUID]
