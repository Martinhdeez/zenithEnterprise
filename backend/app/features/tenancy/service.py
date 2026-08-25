from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.common.exceptions import ConflictError, NotFoundError
from app.core.database import owner_session
from app.features.auth.onboarding.provisioning import seed_system_roles
from app.features.labels.provisioning import seed_default_label, seed_quarantine_label
from app.features.tenancy.context import TenantContext
from app.features.tenancy.model import Tenant


class TenantService:
    """Privileged tenant operations.

    Every method here runs on the owner connection, which **bypasses RLS**. That is
    unavoidable: a tenant has to exist before anything can be scoped to it, so the
    cold-start path cannot itself be scoped.

    It is confined to one class so the bypass surface is one file rather than a habit.
    Nothing served over HTTP may call this — see `mvp.md` §2.4, where the CLI is the
    only supported caller.
    """

    async def create(self, name: str) -> Tenant:
        """Create a tenant with its two system roles.

        The roles are seeded in the same transaction on purpose: a tenant that exists
        without them is a tenant nobody can administer, and leaving that window open
        means a failure halfway through produces exactly that.
        """
        async with owner_session() as session:
            tenant = Tenant(name=name)
            session.add(tenant)
            try:
                await session.flush()
            except IntegrityError as exc:
                # UNIQUE(name). Surfaced as a domain error so the CLI can print
                # something a person understands instead of a driver traceback.
                raise ConflictError(f"a tenant named {name!r} already exists") from exc
            roles = await seed_system_roles(session, tenant.id)
            await seed_default_label(session, tenant.id, list(roles.values()))
            # The two reserved labels are created together because an upload needs both:
            # the quarantine to land in, and the default to be filed into when the
            # classifier declines or is not configured.
            await seed_quarantine_label(session, tenant.id, list(roles.values()))
            await session.refresh(tenant)
            return tenant

    async def by_name(self, name: str) -> Tenant:
        async with owner_session() as session:
            tenant = await session.scalar(select(Tenant).where(Tenant.name == name))
            if tenant is None:
                raise NotFoundError(f"no tenant named {name!r}")
            return tenant

    async def list_all(self) -> list[Tenant]:
        async with owner_session() as session:
            result = await session.scalars(select(Tenant).order_by(Tenant.created_at))
            return list(result)

    async def context_for(self, tenant_id: UUID) -> TenantContext:
        """Builds a context with no labels.

        Deliberately the narrow one. Labels are resolved from the user's roles, which
        do not exist until F2, and defaulting to "everything" here would mean the
        first caller to forget label resolution silently sees the whole tenant.
        """
        return TenantContext(tenant_id=tenant_id)
