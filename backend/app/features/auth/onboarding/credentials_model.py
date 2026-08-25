from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, created_at, uuid_col, uuid_pk


class CredentialToken(Base):
    """A single-use link that lets somebody set their own password.

    Replaces handing out a generated password. The distinction that matters is what ends up
    in a chat log: a password works forever and is the user's real credential, while this
    is spent the moment it is used and expires on its own.

    **The row never holds the token.** `token_hash` is sha256 of it, so a database dump —
    a backup on a laptop, a support export — is not a set of working links into every
    account. Lookup is by hash, so nothing is lost by storing only that.

    Reads and writes from the unauthenticated set-password route do not go through this
    model: that request has no tenant to bind a session to, so RLS has nothing to filter on,
    and it goes through the two SECURITY DEFINER functions in migration 0016 instead. This
    model covers the authenticated side — an administrator issuing a link — and gives
    Alembic the table.
    """

    __tablename__ = "credential_tokens"
    __table_args__ = (
        CheckConstraint("purpose IN ('invitation', 'reset')", name="ck_credential_tokens_purpose"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid_col] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    user_id: Mapped[uuid_col] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    #: sha256 of the token. Unique, because that is what a lookup keys on.
    token_hash: Mapped[str] = mapped_column(unique=True)
    #: `invitation` or `reset`. The set-password page says different things for each, and
    #: the audit trail distinguishes creating an account from recovering one.
    purpose: Mapped[str]
    # Explicitly timezone-aware, matching the migration: a bare `datetime` annotation
    # maps to TIMESTAMP WITHOUT TIME ZONE, and an expiry that silently loses its offset
    # is an expiry that means different things in different deployments.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Set on use. Nullable rather than a boolean, because the question asked afterwards is
    #: *when* it was redeemed, which a boolean cannot answer.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid_col | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[created_at]
