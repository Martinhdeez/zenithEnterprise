"""Test infrastructure: real Postgres, never SQLite.

SQLite has no pgvector, no RLS and no tsvector. A test passing against SQLite says
nothing about the only things that have to be guaranteed here.
"""

import asyncio
import os

# Set before anything imports `app`, because `Settings` is constructed at import time and
# now refuses to build without a real secret. Tests supply their own rather than relying
# on a default, which is the whole point: there is no default any more, on any machine.
os.environ.setdefault("ZENITH_JWT_SECRET", "test-secret-" + "x" * 32)

import subprocess  # noqa: E402
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from testcontainers.community.postgres import PostgresContainer

BACKEND_DIR = Path(__file__).resolve().parent
IMAGE = "paradedb/paradedb:0.15.26-pg17"
APP_PASSWORD = "app-test"


def _async_url(container: PostgresContainer, user: str, password: str) -> str:
    host = container.get_container_host_ip()
    port = container.get_exposed_port(5432)
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{container.dbname}"


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    """One container per session: starting it per test would multiply CI time."""
    with PostgresContainer(IMAGE, driver="psycopg") as container:
        yield container


@pytest.fixture(scope="session")
def owner_url(postgres: PostgresContainer) -> str:
    return _async_url(postgres, postgres.username, postgres.password)


@pytest.fixture(scope="session")
def migrated(postgres: PostgresContainer, owner_url: str) -> str:
    """Apply migrations and grant the application role access.

    The migration creates `zenith_app` without login: it is the installer that gives
    it credentials. This does the same thing the installer will do.
    """
    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env={**os.environ, "ZENITH_DATABASE_OWNER_URL": owner_url},
        capture_output=True,
        text=True,
    )
    if result.returncode:
        # Without this the failure surfaces as a bare CalledProcessError and every
        # test in the run errors with no reason given.
        pytest.fail(f"alembic upgrade failed:\n{result.stdout}\n{result.stderr}", pytrace=False)
    engine = create_async_engine(owner_url)

    async def _grant_credentials() -> None:
        async with engine.begin() as conn:
            await conn.execute(text(f"ALTER ROLE zenith_app LOGIN PASSWORD '{APP_PASSWORD}'"))
        await engine.dispose()

    asyncio.run(_grant_credentials())
    return _async_url(postgres, "zenith_app", APP_PASSWORD)


@pytest.fixture
async def app_engine(migrated: str) -> AsyncIterator[AsyncEngine]:
    """Engine using the application role: subject to RLS, exactly as in production."""
    engine = create_async_engine(migrated)
    yield engine
    await engine.dispose()


@pytest.fixture
async def owner_engine(migrated: str, owner_url: str) -> AsyncIterator[AsyncEngine]:
    """Engine using the owner: bypasses RLS. Only for seeding test data."""
    engine = create_async_engine(owner_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def seed_session(owner_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(owner_engine) as session:
        yield session


@pytest.fixture
def configured_engines(migrated: str, owner_url: str) -> Iterator[None]:
    """Point the application's own engines at the test container.

    Anything exercising `tenant_session` or `owner_session` goes through the module
    level factories, so they have to be redirected or the test would talk to
    whatever `.env` happens to say.
    """
    from app.core.database import configure_engine, configure_owner_engine

    configure_engine(migrated)
    configure_owner_engine(owner_url)
    yield


@pytest.fixture(autouse=True)
def fresh_login_allowance() -> None:
    """Reset the login rate limiter before every test.

    It is process-wide state deliberately — one limiter per worker is the design — but in
    a test run every case shares it, and every case that logs in spends from the same
    allowance. Without this, the suite passes until it crosses ten logins and then fails
    in whichever test happens to run eleventh, which looks like a bug in that test.
    """
    from app.features.auth.throttle import login_limiter

    login_limiter.reset()


# --- A provisioned tenant -----------------------------------------------------------
#
# Lives here rather than in the auth feature because both the feature tests and the
# API integration tests need it, and pytest only shares fixtures downwards from the
# directory a conftest sits in.

PASSWORD = "correct-horse-battery"


@dataclass(frozen=True, slots=True)
class Account:
    tenant_id: UUID
    admin_id: UUID
    admin_email: str
    member_id: UUID
    member_email: str
    finance_label: UUID


@pytest.fixture
async def account(configured_engines: None) -> Account:
    """A tenant with an admin and a member, built the way the install CLI will build it.

    Deliberately seeded through the owner connection rather than through the code under
    test: a fixture built with the thing it is checking can only prove that thing
    agrees with itself.

    It returns rather than yields, so the seeding transaction is committed before the
    test runs. Holding it open would make the rows invisible to every other connection,
    and the login lookup — which opens its own — would find nothing.
    """
    from app.core.database import owner_session
    from app.features.auth.model import Role
    from app.features.auth.provisioning import create_user
    from app.features.labels.model import AccessLabel, RoleLabel
    from app.features.tenancy.service import TenantService

    tenant = await TenantService().create(f"Auth {uuid4()}")

    async with owner_session() as session:
        roles = {
            role.name: role
            for role in await session.scalars(select(Role).where(Role.tenant_id == tenant.id))
        }
        finance = AccessLabel(tenant_id=tenant.id, name="Finance")
        session.add(finance)
        await session.flush()
        session.add(RoleLabel(role_id=roles["admin"].id, label_id=finance.id))

        admin_email = f"admin-{uuid4()}@example.com"
        member_email = f"member-{uuid4()}@example.com"
        admin = await create_user(session, tenant.id, admin_email, PASSWORD, [roles["admin"].id])
        member = await create_user(session, tenant.id, member_email, PASSWORD, [roles["member"].id])

        account = Account(
            tenant_id=tenant.id,
            admin_id=admin.id,
            admin_email=admin_email,
            member_id=member.id,
            member_email=member_email,
            finance_label=finance.id,
        )

    return account


@pytest.fixture
async def finance_label(account: Account) -> UUID:
    return account.finance_label


@pytest.fixture
async def labelled_document(account: Account) -> UUID:
    """A document carrying the Finance label, with two chunks.

    Inserted through the owner connection rather than through an upload, because upload is
    F4 and the sync behaviour is F3's to guarantee. The chunks matter: they carry their own
    copy of the labels, and a chunk that disagrees with its document is a passage
    retrievable by someone who cannot open the file it came from.
    """
    from sqlalchemy import text

    from app.core.database import owner_session

    async with owner_session() as session:
        document_id = await session.scalar(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes) "
                "VALUES (:t, 'labelled.pdf', :sha, 1024) RETURNING id"
            ),
            {"t": account.tenant_id, "sha": str(uuid4())},
        )
        await session.execute(
            text("INSERT INTO document_labels (document_id, label_id) VALUES (:d, :l)"),
            {"d": document_id, "l": account.finance_label},
        )
        for page in (1, 2):
            await session.execute(
                text(
                    "INSERT INTO chunks (document_id, tenant_id, page_num, char_start, "
                    "char_end, text) VALUES (:d, :t, :p, 0, 10, 'chunk text')"
                ),
                {"d": document_id, "t": account.tenant_id, "p": page},
            )

    assert isinstance(document_id, UUID)
    return document_id


@pytest.fixture
async def extra_label_for_member(account: Account) -> UUID:
    """A second role carrying a second label, to check a user gets the union."""
    from app.core.database import owner_session
    from app.features.auth.model import Role, UserRole
    from app.features.labels.model import AccessLabel, RoleLabel

    async with owner_session() as session:
        label = AccessLabel(tenant_id=account.tenant_id, name=f"HR {uuid4()}")
        role = Role(tenant_id=account.tenant_id, name=f"people-{uuid4()}")
        session.add_all([label, role])
        await session.flush()
        session.add_all(
            [
                RoleLabel(role_id=role.id, label_id=label.id),
                UserRole(user_id=account.member_id, role_id=role.id),
            ]
        )
        return label.id
