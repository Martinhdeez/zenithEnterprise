"""Purge with the queue installed exactly as the installer installs it.

An independent container prevents these owner-installed queue tables from masking the
queue-absent path in the rest of the lifecycle suite. No queue grants are added.
"""

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID

import procrastinate
import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.community.postgres import PostgresContainer

from app.core.config import settings
from app.core.database import (
    configure_engine,
    configure_owner_engine,
    configure_platform_engine,
    dispose_engines,
    get_owner_session_factory,
    get_platform_session_factory,
    get_session_factory,
    owner_session,
    platform_session,
)
from app.features.system.purge import purge_tenant
from app.features.system.service import SystemService
from app.features.system.tests.test_lifecycle import document_with_file, rows_for
from app.features.tenancy.service import TenantService
from conftest import (
    APP_PASSWORD,
    BACKEND_DIR,
    IMAGE,
    MAX_LOCKS_PER_TRANSACTION,
    PLATFORM_PASSWORD,
)


@pytest.fixture(scope="module")
def queue_database() -> Iterator[tuple[str, str, str]]:
    """Real migrated Postgres, with the same bounded resources as the QA database."""
    with (
        PostgresContainer(IMAGE, driver="psycopg")
        .with_command(f"postgres -c max_locks_per_transaction={MAX_LOCKS_PER_TRANSACTION}")
        .with_kwargs(mem_limit="768m", nano_cpus=2_000_000_000) as container
    ):
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        base = f"{host}:{port}/{container.dbname}"
        owner_url = f"postgresql+psycopg://{container.username}:{container.password}@{base}"
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=BACKEND_DIR,
            env={**os.environ, "ZENITH_DATABASE_OWNER_URL": owner_url},
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        yield (
            owner_url,
            f"postgresql+psycopg://zenith_app:{APP_PASSWORD}@{base}",
            f"postgresql+psycopg://zenith_platform:{PLATFORM_PASSWORD}@{base}",
        )


@pytest.fixture
async def installed_queue(queue_database: tuple[str, str, str]) -> AsyncIterator[None]:
    previous_urls: list[str] = []
    for factory in (
        get_session_factory(),
        get_owner_session_factory(),
        get_platform_session_factory(),
    ):
        async with factory() as session:
            assert isinstance(session.bind, AsyncEngine)
            previous_urls.append(session.bind.url.render_as_string(hide_password=False))
    owner_url, app_url, platform_url = queue_database
    engine = create_async_engine(owner_url)
    async with engine.begin() as connection:
        await connection.execute(text(f"ALTER ROLE zenith_app LOGIN PASSWORD '{APP_PASSWORD}'"))
        await connection.execute(
            text(f"ALTER ROLE zenith_platform LOGIN PASSWORD '{PLATFORM_PASSWORD}'")
        )
    await engine.dispose()
    configure_engine(app_url)
    configure_owner_engine(owner_url)
    configure_platform_engine(platform_url)
    queue = procrastinate.App(
        connector=procrastinate.PsycopgConnector(
            conninfo=owner_url.replace("postgresql+psycopg://", "postgresql://")
        )
    )
    try:
        # The exact operation performed by `zenith install-queue`; not a fake table.
        async with queue.open_async():
            await queue.schema_manager.apply_schema_async()
        yield
    finally:
        await dispose_engines()
        configure_engine(previous_urls[0])
        configure_owner_engine(previous_urls[1])
        configure_platform_engine(previous_urls[2])
        await dispose_engines()


async def queued_job(tenant_id: UUID, status: str = "todo") -> int:
    async with owner_session() as session:
        job_id = await session.scalar(
            text(
                "INSERT INTO procrastinate_jobs (queue_name, task_name, args, status) "
                "VALUES ('ingestion', 'ingest_document', "
                "jsonb_build_object('tenant_id', CAST(:t AS text)), "
                "CAST(:s AS procrastinate_job_status)) RETURNING id"
            ),
            {"t": str(tenant_id), "s": status},
        )
        assert job_id is not None
        return int(job_id)


async def test_installed_queue_purge_cancels_only_target_todo_jobs(
    installed_queue: None,
) -> None:
    target = await TenantService().create("Installed queue purge target")
    neighbor = await TenantService().create("Installed queue purge neighbor")
    target_sha = await document_with_file(target.id)
    neighbor_sha = await document_with_file(neighbor.id)
    target_file = Path(settings.storage_dir) / str(target.id) / f"{target_sha}.pdf"
    neighbor_file = Path(settings.storage_dir) / str(neighbor.id) / f"{neighbor_sha}.pdf"
    target_todo = [await queued_job(target.id), await queued_job(target.id)]
    target_doing = await queued_job(target.id, "doing")
    target_failed = await queued_job(target.id, "failed")
    neighbor_todo = await queued_job(neighbor.id)

    # The real installation does not grant the platform connection queue access.
    with pytest.raises(ProgrammingError, match="permission denied for table procrastinate_jobs"):
        async with platform_session() as session:
            await session.execute(
                text("UPDATE procrastinate_jobs SET status = 'cancelled' WHERE id = :id"),
                {"id": target_todo[0]},
            )

    result = await purge_tenant(target.id)

    assert result == {"files": 1, "jobs_cancelled": 2}
    assert all(count == 0 for count in (await rows_for(target.id)).values())
    assert not target_file.exists()
    assert (await rows_for(neighbor.id))["documents"] == 1
    assert neighbor_file.exists()
    assert (await SystemService()._one(target.id)).status == "purged"  # type: ignore[reportPrivateUsage]
    assert (await SystemService()._one(neighbor.id)).status == "active"  # type: ignore[reportPrivateUsage]
    async with owner_session() as session:
        jobs = dict(
            (await session.execute(text("SELECT id, status FROM procrastinate_jobs")))
            .tuples()
            .all()
        )
    assert jobs == {
        **dict.fromkeys(target_todo, "cancelled"),
        target_doing: "doing",
        target_failed: "failed",
        neighbor_todo: "todo",
    }
    # Retrying the purge neither recalls an active job nor damages the neighbor.
    assert await purge_tenant(target.id) == {"files": 0, "jobs_cancelled": 0}
    async with platform_session() as session:
        assert not await session.scalar(
            text("SELECT has_table_privilege(current_user, 'audit_events', 'UPDATE')")
        )
        assert not await session.scalar(
            text("SELECT has_table_privilege(current_user, 'audit_events', 'DELETE')")
        )
