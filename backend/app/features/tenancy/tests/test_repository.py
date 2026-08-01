"""The repository guard: no query runs without an RLS context.

The guard has to be tested in **both** directions. Only checking that it rejects a
bad session lets a version through that rejects everything — which is exactly the bug
this file was written after finding.
"""

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.common.exceptions import MissingTenantContextError
from app.core.database import tenant_session
from app.features.tenancy.context import TenantContext
from app.features.tenancy.repository import TenantRepository
from app.features.tenancy.service import TenantService

pytestmark = pytest.mark.asyncio


async def test_repository_refuses_a_session_without_context(app_engine: AsyncEngine) -> None:
    """A raw session has no context, so building a repository on it must fail loudly.

    Postgres would return zero rows silently, and a silent zero is hard to debug."""
    async with AsyncSession(app_engine) as session:
        with pytest.raises(MissingTenantContextError):
            TenantRepository(session)


async def test_repository_accepts_a_session_from_tenant_session(
    configured_engines: None,
) -> None:
    """The other direction. A guard that rejects valid sessions is worse than none:
    it teaches you to distrust it."""
    tenant = await TenantService().create(f"Repository OK {uuid4()}")

    async with tenant_session(TenantContext(tenant_id=tenant.id)) as session:
        repository = TenantRepository(session)

        assert repository.tenant_id == tenant.id
        assert repository.context.label_ids == ()


async def test_repository_reads_its_own_tenant(configured_engines: None) -> None:
    tenant = await TenantService().create(f"Own tenant {uuid4()}")

    async with tenant_session(TenantContext(tenant_id=tenant.id)) as session:
        current = await TenantRepository(session).current()

    assert current is not None
    assert current.id == tenant.id


async def test_repository_cannot_reach_another_tenant(configured_engines: None) -> None:
    """`get()` by primary key bypasses no policy: RLS filters the row out."""
    mine = await TenantService().create(f"Mine {uuid4()}")
    theirs = await TenantService().create(f"Theirs {uuid4()}")

    async with tenant_session(TenantContext(tenant_id=mine.id)) as session:
        found = await TenantRepository(session).get(theirs.id)

    assert found is None
