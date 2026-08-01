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


class MeResponse(BaseModel):
    user_id: UUID
    tenant_id: UUID
    permissions: list[str]
    # The labels this caller reaches. Returned so the UI can hide what the user cannot
    # open, never so it can decide: the decision is RLS's, in the database.
    label_ids: list[UUID]
