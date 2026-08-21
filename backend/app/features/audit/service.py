"""Who changed who could see what.

The existing "Audit Log" screen reads `queries` — the record of questions asked. Useful,
and not an audit trail. Nothing recorded who granted a role, who moved somebody into a
group, who raised a clearance, or who purged an organisation, which is the first thing a
compliance reviewer asks about a product sold on access control.

**Recording is best-effort; the change is not.** `record` never raises. A failure to write
the audit row must not roll back the grant that succeeded, because the alternative — an
administrator whose permission change fails because a log table is full — is a worse
product and, perversely, a worse security posture: people work around tools that refuse to
work. The failure is logged loudly instead, and the append-only grant means a row that did
get written cannot later be quietly removed.

**Written in its own transaction, after the change commits.** Sharing the caller's session
would tie the record to the outcome in the other direction: a rollback for any later reason
would erase the evidence along with the change. Recording only what actually committed is
the property worth having.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text

from app.core.database import platform_session, tenant_session
from app.features.auth.service import AccessProfile
from app.features.documents.pagination import Cursor, clamp

log = structlog.get_logger()

#: Reading the trail. Held apart from `query.history.any` on purpose: somebody who may read
#: what colleagues asked is not automatically somebody who may read who granted what.
READ = "audit.read"

PAGE = 20


@dataclass(frozen=True, slots=True)
class AuditEvent:
    id: UUID
    actor_email: str
    action: str
    target_type: str | None
    target_id: UUID | None
    target_name: str | None
    details: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AuditPage:
    events: list[AuditEvent]
    next_cursor: str | None


INSERT = (
    "INSERT INTO audit_events "
    "(tenant_id, actor_user_id, actor_email, action, target_type, target_id, target_name, details) "
    "VALUES (:tenant_id, :actor, :actor_email, :action, :target_type, :target_id, :target_name, "
    "CAST(:details AS jsonb))"
)


async def record(
    profile: AccessProfile,
    action: str,
    *,
    target_type: str | None = None,
    target_id: UUID | None = None,
    target_name: str | None = None,
    **details: Any,
) -> None:
    """Append one event. Never raises.

    `action` is a stable `subject.verb` token — `role.created`, `group.members_set` — and
    never a rendered sentence. The screen writes the prose; a log full of English is a log
    nothing can filter, count or alert on.
    """
    try:
        async with tenant_session(profile.context) as session:
            await session.execute(
                text(INSERT),
                {
                    "tenant_id": profile.context.tenant_id,
                    "actor": profile.user_id,
                    "actor_email": profile.email,
                    "action": action,
                    "target_type": target_type,
                    "target_id": target_id,
                    "target_name": target_name,
                    "details": json.dumps(details, default=str),
                },
            )
    except Exception:  # noqa: BLE001 — see the module docstring: the change already committed.
        log.exception("audit.write_failed", action=action, actor=str(profile.user_id))


async def record_system(
    actor_id: UUID,
    actor_email: str,
    action: str,
    *,
    tenant_id: UUID | None = None,
    target_name: str | None = None,
    **details: Any,
) -> None:
    """An event above every tenant: provisioning, suspension, purging.

    Written through `platform_session` because there is no tenant context to write it in —
    a system administrator acting on somebody else's organisation is precisely the case
    ordinary RLS is built to prevent. `tenant_id` is stamped so the event still belongs to
    the organisation it acted on, and survives that organisation's deletion because the
    column is `ON DELETE SET NULL`.
    """
    try:
        async with platform_session() as session:
            await session.execute(
                text(INSERT),
                {
                    "tenant_id": tenant_id,
                    "actor": actor_id,
                    "actor_email": actor_email,
                    "action": action,
                    "target_type": "tenant",
                    "target_id": tenant_id,
                    "target_name": target_name,
                    "details": json.dumps(details, default=str),
                },
            )
    except Exception:  # noqa: BLE001
        log.exception("audit.write_failed", action=action, actor=str(actor_id))


class AuditService:
    def __init__(self, profile: AccessProfile) -> None:
        self.profile = profile

    async def page(self, cursor: str | None = None, limit: int | None = None) -> AuditPage:
        """One page of the trail, newest first.

        Keyset, like every other list in this system: `OFFSET n` makes Postgres evaluate the
        policy on every row it then discards, and an offset repeats or skips rows whenever
        something is written between two pages — which, for a log, is exactly when somebody
        is reading it.
        """
        size = clamp(limit or PAGE)
        after = Cursor.decode(cursor) if cursor else None

        where = ""
        parameters: dict[str, object] = {"limit": size + 1}
        if after:
            where = "WHERE (created_at, id) < (:after_at, :after_id) "
            parameters |= {"after_at": after.created_at, "after_id": after.id}

        async with tenant_session(self.profile.context) as session:
            rows = list(
                await session.execute(
                    text(
                        "SELECT id, actor_email, action, target_type, target_id, target_name, "
                        "details, created_at FROM audit_events "
                        f"{where}"
                        "ORDER BY created_at DESC, id DESC LIMIT :limit"
                    ),
                    parameters,
                )
            )

        more = len(rows) > size
        events = [
            AuditEvent(
                id=row.id,
                actor_email=row.actor_email,
                action=row.action,
                target_type=row.target_type,
                target_id=row.target_id,
                target_name=row.target_name,
                details=row.details or {},
                created_at=row.created_at,
            )
            for row in rows[:size]
        ]
        last = rows[size - 1] if more else None
        return AuditPage(
            events=events,
            next_cursor=Cursor(last.created_at, last.id).encode() if last else None,
        )


__all__ = ["READ", "AuditEvent", "AuditPage", "AuditService", "record", "record_system"]
