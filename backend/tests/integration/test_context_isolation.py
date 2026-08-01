"""The assumption the whole isolation model rests on.

`set_config(..., true)` is documented as transaction-local. Everything else in this
system trusts that: if it were connection-local instead, a pooled connection handed
back after one tenant's request would carry their context into the next tenant's
request, and RLS would cheerfully enforce the wrong tenant.

That is the worst bug this system can have, it would be invisible under low load, and
it costs one test to rule out. So it gets a test.
"""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.core.database import configure_engine, tenant_session
from app.features.tenancy.context import TenantContext
from app.features.tenancy.service import TenantService

pytestmark = pytest.mark.asyncio


async def _visible_tenant_names(context: TenantContext) -> set[str]:
    async with tenant_session(context) as session:
        # A pause inside the transaction so the two coroutines genuinely overlap
        # rather than running one after the other.
        await asyncio.sleep(0.05)
        return set(await session.scalars(text("SELECT name FROM tenants")))


async def test_concurrent_contexts_do_not_leak_into_each_other(
    configured_engines: None,
) -> None:
    first = await TenantService().create(f"Concurrent A {uuid4()}")
    second = await TenantService().create(f"Concurrent B {uuid4()}")

    seen_first, seen_second = await asyncio.gather(
        _visible_tenant_names(TenantContext(tenant_id=first.id)),
        _visible_tenant_names(TenantContext(tenant_id=second.id)),
    )

    assert seen_first == {first.name}
    assert seen_second == {second.name}


async def test_context_does_not_survive_on_a_reused_connection(
    configured_engines: None, migrated: str
) -> None:
    """Same danger, different shape: sequential requests over a pool of one.

    With `pool_size=1` the second transaction is guaranteed to get the very same
    connection the first one used. If the GUC outlived the transaction, the second
    context would inherit the first one's tenant.
    """
    first = await TenantService().create(f"Reused A {uuid4()}")
    second = await TenantService().create(f"Reused B {uuid4()}")

    configure_engine(migrated, pool_size=1)
    try:
        seen_first = await _visible_tenant_names(TenantContext(tenant_id=first.id))
        seen_second = await _visible_tenant_names(TenantContext(tenant_id=second.id))
    finally:
        configure_engine(migrated)

    assert seen_first == {first.name}
    assert seen_second == {second.name}


async def test_no_context_leaks_after_the_transaction_ends(
    configured_engines: None, migrated: str
) -> None:
    """A session opened with no context, on a connection that previously carried one,
    must see nothing."""
    tenant = await TenantService().create(f"Leftover {uuid4()}")

    configure_engine(migrated, pool_size=1)
    try:
        await _visible_tenant_names(TenantContext(tenant_id=tenant.id))

        from app.core.database import get_session_factory

        async with get_session_factory()() as session:
            leftover: UUID | None = await session.scalar(
                text("SELECT nullif(current_setting('zenith.tenant_id', true), '')::uuid")
            )
            visible = await session.scalar(text("SELECT count(*) FROM tenants"))
    finally:
        configure_engine(migrated)

    assert leftover is None
    assert visible == 0
