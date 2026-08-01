"""Test infrastructure: real Postgres, never SQLite.

SQLite has no pgvector, no RLS and no tsvector. A test passing against SQLite says
nothing about the only things that have to be guaranteed here.
"""

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
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
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env={**os.environ, "ZENITH_DATABASE_OWNER_URL": owner_url},
        check=True,
        capture_output=True,
    )
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
