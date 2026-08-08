"""The lifecycle of an organisation, from the one place that can see all of them.

Every method here runs on `platform_session()` — RLS bypassed, because the question this
service exists to answer is the one RLS is built to refuse: what is in every tenant at once.
That connection cannot alter the schema, so the blast radius of a mistake here is a
customer's data and never the installation.

**Nothing in this module is reachable without `requires_system_admin`.** The router enforces
that on every route; this file is where the damage would be done if it ever stopped.

The lifecycle is deliberately not a free-for-all state machine:

    active  <->  suspended  ->  purging  ->  purged

Suspension is reversible and touches no data. A purge can only start from `suspended`, so
destroying a customer takes two deliberate acts by somebody who saw the first one take
effect — and the caller must repeat the organisation's name, so neither act is a misclick.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text

from app.common.exceptions import ConflictError, NotFoundError
from app.core.database import platform_session
from app.features.tenancy.model import ACTIVE, PURGED, PURGING, SUSPENDED


@dataclass(frozen=True, slots=True)
class Organisation:
    id: UUID
    name: str
    status: str
    created_at: datetime
    status_changed_at: datetime | None
    users: int
    documents: int
    #: Sum of `documents.size_bytes`. What the row claims, not what the disk holds — a
    #: document whose file went missing still counts here, and that mismatch is worth
    #: seeing rather than papering over with a filesystem walk on every page load.
    storage_bytes: int


@dataclass(frozen=True, slots=True)
class Provisioned:
    """A new organisation and the one-time credential for its first administrator."""

    organisation: Organisation
    admin_email: str
    #: Returned exactly once. Never stored in the clear, never logged, not recoverable.
    password: str


class SystemService:
    async def organisations(self) -> list[Organisation]:
        async with platform_session() as session:
            rows = await session.execute(
                text(
                    # Subqueries rather than joins: a tenant with no users and a tenant with
                    # no documents both have to appear, and three left joins over these
                    # tables multiply each other into wrong counts.
                    "SELECT t.id, t.name, t.status, t.created_at, t.status_changed_at, "
                    "  (SELECT count(*) FROM users u WHERE u.tenant_id = t.id) AS users, "
                    "  (SELECT count(*) FROM documents d WHERE d.tenant_id = t.id) AS documents, "
                    "  (SELECT coalesce(sum(d.size_bytes), 0) FROM documents d "
                    "     WHERE d.tenant_id = t.id) AS storage_bytes "
                    "FROM tenants t ORDER BY t.created_at"
                )
            )
            return [
                Organisation(
                    id=row.id,
                    name=row.name,
                    status=row.status,
                    created_at=row.created_at,
                    status_changed_at=row.status_changed_at,
                    users=int(row.users),
                    documents=int(row.documents),
                    storage_bytes=int(row.storage_bytes),
                )
                for row in rows
            ]

    async def provision(self, name: str, admin_email: str) -> Provisioned:
        """Create an organisation and its first administrator.

        Reuses `TenantService.create` and `create_user` rather than re-seeding roles and the
        default label here — that provisioning already exists for the CLI, and a second copy
        of it is how two ways of creating a tenant start disagreeing about what a new tenant
        contains.
        """
        # Imported inside the method: these reach the model registry, and importing it at
        # module scope makes the order this module is first imported in matter.
        from app.features.auth.provisioning import create_user, generate_password
        from app.features.auth.service import normalise_email
        from app.features.tenancy.service import TenantService

        tenant = await TenantService().create(name)
        password = generate_password()

        async with platform_session() as session:
            role_id = await session.scalar(
                text("SELECT id FROM roles WHERE tenant_id = :t AND name = 'admin'"),
                {"t": tenant.id},
            )
            assert role_id is not None, "provisioning must seed an admin role"
            await create_user(session, tenant.id, admin_email, password, [UUID(str(role_id))])

        return Provisioned(
            organisation=await self._one(tenant.id),
            admin_email=normalise_email(admin_email),
            password=password,
        )

    async def suspend(self, tenant_id: UUID) -> Organisation:
        """Cut off access without touching a byte of data.

        Reversible, and that is the whole point of its existing: it is the answer to
        "stop this customer now" that does not require deciding whether they are coming back.
        """
        return await self._move(tenant_id, to=SUSPENDED, allowed={ACTIVE, SUSPENDED})

    async def activate(self, tenant_id: UUID) -> Organisation:
        return await self._move(tenant_id, to=ACTIVE, allowed={ACTIVE, SUSPENDED})

    async def begin_purge(self, tenant_id: UUID, confirm_name: str) -> Organisation:
        """Mark an organisation for destruction, having checked the two brakes.

        Neither brake is in the UI alone. A wrong deployment of the front end must not be
        able to destroy a customer by itself, so both are re-checked here:

        1. **It must already be suspended.** Purging is one way, and requiring a prior,
           reversible act means nobody reaches it without having seen the consequence of
           the first one.
        2. **The caller must repeat the name.** Not the id — an id is copied and pasted
           without being read.

        The work itself runs in the background; this only opens the door. See `purge.py`.
        """
        current = await self._one(tenant_id)

        if current.status != SUSPENDED:
            raise ConflictError(
                f"{current.name!r} is {current.status}. Suspend it first — purging is not "
                f"reversible, and suspension is."
            )
        if confirm_name != current.name:
            raise ConflictError("the name does not match this organisation")

        return await self._move(tenant_id, to=PURGING, allowed={SUSPENDED})

    async def _move(self, tenant_id: UUID, *, to: str, allowed: set[str]) -> Organisation:
        current = await self._one(tenant_id)
        if current.status not in allowed:
            raise ConflictError(f"an organisation that is {current.status} cannot become {to}")
        if current.status == to:
            return current

        async with platform_session() as session:
            await session.execute(
                text("UPDATE tenants SET status = :s, status_changed_at = :at WHERE id = :t"),
                {"s": to, "at": datetime.now(UTC), "t": tenant_id},
            )
        return await self._one(tenant_id)

    async def _one(self, tenant_id: UUID) -> Organisation:
        found = [org for org in await self.organisations() if org.id == tenant_id]
        if not found:
            raise NotFoundError(f"no organisation {tenant_id}")
        return found[0]


#: Re-exported so callers do not import from two places to talk about one lifecycle.
__all__ = [
    "ACTIVE",
    "PURGED",
    "PURGING",
    "SUSPENDED",
    "Organisation",
    "Provisioned",
    "SystemService",
]
