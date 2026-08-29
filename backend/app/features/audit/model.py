from typing import Any

from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, created_at, uuid_col, uuid_pk


class AuditEvent(Base):
    """One change to who can see what.

    Distinct from `Query`, which records a question somebody asked. The two lived under one
    word — the screen called "Audit Log" reads `queries` — and that was the gap: a product
    sold on access control kept no record of access being granted.

    **Nothing writes to this through the ORM by design.** Migration 0015 leaves `zenith_app`
    with INSERT and SELECT and no UPDATE or DELETE, so an `AuditEvent` fetched, modified and
    flushed would fail at the database. The model exists so Alembic sees the table in one
    `MetaData` and so reads have types; writes go through `audit.service.record`, which is
    one INSERT and no identity map.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        # The only read this table has: one tenant's log, newest first, in keyset pages.
        Index(
            "ix_audit_events_tenant_created",
            "tenant_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )

    id: Mapped[uuid_pk]
    #: NULL for events above every tenant — provisioning, suspension, purging. `SET NULL`
    #: rather than CASCADE so purging an organisation cannot destroy the record of the purge.
    tenant_id: Mapped[uuid_col | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL")
    )
    actor_user_id: Mapped[uuid_col | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    #: Copied at write time, so the row still names the actor after the actor is deleted —
    #: which is exactly the case an audit trail exists for.
    actor_email: Mapped[str]
    #: A stable `subject.verb` token, never a sentence. Prose here would be a log nothing
    #: can filter, count or alert on.
    action: Mapped[str]
    target_type: Mapped[str | None]
    target_id: Mapped[uuid_col | None]
    #: What the target was called at the time. Renaming or deleting it afterwards must not
    #: leave a row naming a uuid nobody can resolve.
    target_name: Mapped[str | None]
    #: Shaped per action: a permission change carries two lists, a clearance change an
    #: integer. Columns would have to be the union of every action's shape.
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[created_at]
