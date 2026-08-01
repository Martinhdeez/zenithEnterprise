from app.common.repositories.base import ScopedRepository
from app.features.tenancy.model import Tenant


class TenantRepository(ScopedRepository[Tenant]):
    """Reads the tenant of the current context.

    There is deliberately no `create` here. Creating a tenant cannot happen inside a
    tenant context — none exists yet — so it lives in `TenantService`, which uses the
    owner connection. Keeping the two apart means the one operation that escapes RLS
    is not reachable from a request-scoped session by accident.
    """

    model = Tenant

    async def current(self) -> Tenant | None:
        """The context's own tenant. RLS makes any other row invisible, so this
        cannot return someone else's even if the id were wrong."""
        return await self.get(self.tenant_id)
