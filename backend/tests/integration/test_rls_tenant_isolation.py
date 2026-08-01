"""MVP gate: cross-tenant data leakage, 0 cases.

Everything runs as `zenith_app`, the role the application uses in production. A test
querying as the owner would prove nothing: the owner bypasses the policies.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

pytestmark = pytest.mark.asyncio


async def _seed(engine: AsyncEngine) -> tuple[UUID, UUID]:
    """Two tenants with one document each. Seeded as the owner."""
    tenant_a, tenant_b = uuid4(), uuid4()
    async with engine.begin() as conn:
        for tenant, name in ((tenant_a, "Company A"), (tenant_b, "Company B")):
            await conn.execute(
                text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
                {"id": tenant, "name": f"{name} {tenant}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO documents (tenant_id, filename, sha256, size_bytes) "
                    "VALUES (:tenant, :filename, :sha, 10)"
                ),
                {"tenant": tenant, "filename": f"{name}.pdf", "sha": str(uuid4())},
            )
    return tenant_a, tenant_b


async def _with_context(session: AsyncSession, tenant: UUID) -> None:
    await session.execute(
        text("SELECT set_config('zenith.tenant_id', :t, true)"), {"t": str(tenant)}
    )


async def test_a_tenant_cannot_see_another_tenants_documents(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    tenant_a, _ = await _seed(owner_engine)

    async with AsyncSession(app_engine) as session, session.begin():
        await _with_context(session, tenant_a)
        visible = set(await session.scalars(text("SELECT filename FROM documents")))

    assert visible == {"Company A.pdf"}


async def test_nothing_is_visible_without_context(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The failure is closed: forgetting the context hides data, it never exposes it."""
    await _seed(owner_engine)

    async with AsyncSession(app_engine) as session, session.begin():
        documents = await session.scalar(text("SELECT count(*) FROM documents"))
        tenants = await session.scalar(text("SELECT count(*) FROM tenants"))

    assert documents == 0
    assert tenants == 0


async def test_cannot_write_into_another_tenant(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """WITH CHECK closes the other half: read isolation is worthless if writes can
    plant a row in the neighbour's tenant."""
    tenant_a, tenant_b = await _seed(owner_engine)

    async with AsyncSession(app_engine) as session, session.begin():
        await _with_context(session, tenant_a)
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "INSERT INTO documents (tenant_id, filename, sha256, size_bytes) "
                    "VALUES (:tenant, 'intruder.pdf', :sha, 10)"
                ),
                {"tenant": tenant_b, "sha": str(uuid4())},
            )


async def test_cannot_delete_from_another_tenant(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    tenant_a, tenant_b = await _seed(owner_engine)

    async with AsyncSession(app_engine) as session, session.begin():
        await _with_context(session, tenant_a)
        deleted = await session.scalar(
            text(
                "WITH deleted AS (DELETE FROM documents WHERE tenant_id = :tenant "
                "RETURNING 1) SELECT count(*) FROM deleted"
            ),
            {"tenant": tenant_b},
        )
        assert deleted == 0

    async with owner_engine.begin() as conn:
        remaining = await conn.scalar(
            text("SELECT count(*) FROM documents WHERE tenant_id = :t"), {"t": tenant_b}
        )
    assert remaining == 1


async def test_startup_rejects_owner_credentials(
    owner_engine: AsyncEngine, migrated: str, owner_url: str
) -> None:
    """The schema does not use FORCE ROW LEVEL SECURITY, so the owner sees everything.

    Pointing the application at those credentials would disable isolation silently.
    `verify_rls_active` catches it at startup and the process never serves traffic.
    """
    from app.core.database import configure_engine, verify_rls_active

    await _seed(owner_engine)

    configure_engine(owner_url)
    with pytest.raises(RuntimeError, match="RLS is not active"):
        await verify_rls_active()

    # With the right role, startup passes.
    configure_engine(migrated)
    await verify_rls_active()
