import pytest
from sqlalchemy import select

from app.core.database import owner_session
from app.features.auth.model import Permission, Role, RolePermission
from app.features.auth.permissions import ADMINISTRATION, CATALOGUE, SYSTEM_ROLES
from conftest import Account

pytestmark = pytest.mark.asyncio


async def test_catalogue_matches_the_database(configured_engines: None) -> None:
    """The guard on the duplication between `permissions.py` and migration 0002.

    The migration cannot import the module — a migration that imports application code
    stops being reproducible the moment that code moves — so the two lists are copies,
    and copies drift. This is what stops them: a permission added to one and not the
    other fails here rather than in production, where `requires("documents.export")`
    would raise a foreign-key error on a role nobody could ever be granted.
    """
    async with owner_session() as session:
        seeded = {
            permission.code: permission.description
            for permission in await session.scalars(select(Permission))
        }

    assert seeded == CATALOGUE


async def test_admin_holds_the_whole_catalogue(account: Account) -> None:
    """A permission added to the catalogue and forgotten in the seeding is a permission
    nobody in a fresh installation can hold."""
    async with owner_session() as session:
        admin = await session.scalar(
            select(Role).where(Role.tenant_id == account.tenant_id, Role.name == "admin")
        )
        assert admin is not None
        granted = set(
            await session.scalars(
                select(RolePermission.permission_code).where(RolePermission.role_id == admin.id)
            )
        )

    assert granted == set(CATALOGUE)
    assert granted >= ADMINISTRATION


async def test_system_roles_cannot_be_deleted(account: Account) -> None:
    async with owner_session() as session:
        roles = await session.scalars(select(Role).where(Role.tenant_id == account.tenant_id))
        flags = {role.name: role.is_system for role in roles}

    assert flags == dict.fromkeys(SYSTEM_ROLES, True)
