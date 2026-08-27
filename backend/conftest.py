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
# Ingestion is a background concern with its own tests. An upload test must not need a
# worker process and a running embedding service to store a file.
os.environ.setdefault("ZENITH_DISABLE_INGESTION_QUEUE", "1")

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
PLATFORM_PASSWORD = "platform-test"


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
            # Migration 0010 creates this one NOLOGIN too, for the same reason: the
            # credential belongs to the installer, not the repository.
            await conn.execute(
                text(f"ALTER ROLE zenith_platform LOGIN PASSWORD '{PLATFORM_PASSWORD}'")
            )
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


@pytest.fixture(scope="session")
def platform_url(postgres: PostgresContainer, migrated: str) -> str:
    """The role the system panel connects as: bypasses RLS, holds no DDL."""
    return _async_url(postgres, "zenith_platform", PLATFORM_PASSWORD)


@pytest.fixture
def configured_engines(migrated: str, owner_url: str, platform_url: str) -> Iterator[None]:
    """Point the application's own engines at the test container.

    Anything exercising `tenant_session` or `owner_session` goes through the module
    level factories, so they have to be redirected or the test would talk to
    whatever `.env` happens to say.
    """
    from app.core.database import (
        configure_engine,
        configure_owner_engine,
        configure_platform_engine,
    )

    configure_engine(migrated)
    configure_owner_engine(owner_url)
    configure_platform_engine(platform_url)
    yield


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test ever writes to the configured storage directory.

    The default is `/var/lib/zenith/documents`, a real path on a real deployment. A test
    run that reaches it either fails on permissions for reasons unrelated to the test, or
    succeeds and leaves a developer's machine holding fixture PDFs.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "storage_dir", tmp_path / "documents")


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


@pytest.fixture(autouse=True)
def fresh_reranker_breaker() -> None:
    """Reset the reranker circuit before every test.

    Process-wide state by design — whether the reranker is answering is a property of the
    deployment, not of a request — but in a test run every case shares it. Without this, a
    test that deliberately fails the reranker leaves failures on the counter, and the third
    such test opens the circuit and silently changes the behaviour of whatever runs next.
    The suite would pass until someone reordered it.

    Exactly the reasoning behind `fresh_login_allowance`, for exactly the same class of
    state.
    """
    from app.features.retrieval.service import RERANKER_BREAKER

    RERANKER_BREAKER.succeeded()


# --- Embedding doubles --------------------------------------------------------------
#
# Here rather than in a feature's test file because search, reranking and generation all
# need them, and the generation tests were importing one from `retrieval/tests/test_rerank`
# — which made a retrieval test file a dependency of a generation one, and meant renaming a
# class in one feature broke the tests of another.
#
# Named for what they do to the search path rather than called `Stub`, because which one a
# test picks is a decision about which half of the search runs.


class LexicalOnlyEmbedder:
    """No `embed_query` at all, so the dense half degrades and the lexical half answers.

    The absence is the point and it must stay an absence: giving it a method that raises
    would exercise the same path, but a later refactor could catch the exception somewhere
    new and the test would go on passing while measuring something else.
    """

    url = "http://stub"
    transport = None

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail


class WorkingEmbedder:
    """Returns a real vector, so both halves run and the search is not degraded.

    Needed wherever `degraded` is under test for another reason — reranking, generation —
    because with `LexicalOnlyEmbedder` the result is degraded before the code under test is
    reached, and the assertion would pass for the wrong reason.

    The vector encodes nothing: whether BGE-M3 puts the right passage near the query is a
    question for the eval corpus, and `eval/` answers it against 19,533 real chunks.
    """

    url = "http://stub"
    transport = None

    async def embed_query(self, question: str) -> list[float]:
        from app.features.embeddings.client import DIMENSION

        vector = [0.0] * DIMENSION
        vector[0] = 1.0
        return vector


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
    # A second compartment reachable by `admin`. Two are needed wherever a test has to
    # union real labels without either of them being the default, which since 0017 is not a
    # choice but the thing that triggers quarantine.
    hr_label: UUID
    # Seeded by tenant provisioning, reachable by both system roles. Where the classifier
    # files a document when it declines or is not configured — see `labels/provisioning.py`.
    default_label: UUID
    # Also seeded by provisioning, but reachable by `admin` alone. An upload that named no
    # compartment waits here until the classifier files it, so that "unfiled" does not mean
    # "readable by the whole tenant" for the length of an ingestion. See migration 0017.
    quarantine_label: UUID


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
    from app.features.auth.onboarding.provisioning import create_user
    from app.features.labels.model import AccessLabel, RoleLabel
    from app.features.tenancy.service import TenantService

    tenant = await TenantService().create(f"Auth {uuid4()}")

    async with owner_session() as session:
        roles = {
            role.name: role
            for role in await session.scalars(select(Role).where(Role.tenant_id == tenant.id))
        }
        default_label = await session.scalar(
            select(AccessLabel.id).where(AccessLabel.tenant_id == tenant.id, AccessLabel.is_default)
        )
        assert default_label is not None, "provisioning must seed a default label"

        quarantine_label = await session.scalar(
            select(AccessLabel.id).where(
                AccessLabel.tenant_id == tenant.id, AccessLabel.is_quarantine
            )
        )
        assert quarantine_label is not None, "provisioning must seed a quarantine label"

        finance = AccessLabel(tenant_id=tenant.id, name="Finance")
        hr = AccessLabel(tenant_id=tenant.id, name="HR")
        session.add_all([finance, hr])
        await session.flush()
        session.add_all(
            [
                RoleLabel(role_id=roles["admin"].id, label_id=finance.id),
                RoleLabel(role_id=roles["admin"].id, label_id=hr.id),
            ]
        )

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
            hr_label=hr.id,
            default_label=default_label,
            quarantine_label=quarantine_label,
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
