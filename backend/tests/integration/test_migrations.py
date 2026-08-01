"""Verification of the initial migration.

Two distinct properties: that the full cycle is reversible, and that the schema
declared in the models and the one applied in the database do not diverge.
"""

import os
import subprocess
from collections.abc import Iterator

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, create_engine, text
from testcontainers.community.postgres import PostgresContainer

from app.models import Base
from conftest import BACKEND_DIR, IMAGE


@pytest.fixture(scope="module")
def own_container() -> Iterator[PostgresContainer]:
    """A separate container: this module runs `downgrade base`, which would wipe the
    data the RLS tests rely on if it shared an instance."""
    with PostgresContainer(IMAGE, driver="psycopg") as container:
        yield container


def _url(container: PostgresContainer) -> str:
    host = container.get_container_host_ip()
    port = container.get_exposed_port(5432)
    return (
        f"postgresql+psycopg://{container.username}:{container.password}"
        f"@{host}:{port}/{container.dbname}"
    )


def _alembic(command: str, url: str) -> None:
    result = subprocess.run(
        ["uv", "run", "alembic", *command.split()],
        cwd=BACKEND_DIR,
        env={**os.environ, "ZENITH_DATABASE_OWNER_URL": url},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_upgrade_downgrade_upgrade_cycle(own_container: PostgresContainer) -> None:
    """A failed deployment has to be reversible. If downgrade does not work, the only
    way back is restoring a backup."""
    url = _url(own_container)
    _alembic("upgrade head", url)

    engine = create_engine(url)
    with engine.connect() as conn:
        assert _table_count(conn) > 15

    _alembic("downgrade base", url)
    with engine.connect() as conn:
        assert _table_count(conn) == 0

    _alembic("upgrade head", url)
    with engine.connect() as conn:
        assert _table_count(conn) > 15
    engine.dispose()


def test_downgrade_works_with_permissions_already_granted(
    own_container: PostgresContainer,
) -> None:
    """Migration 0002 seeds `permissions`, and its downgrade deletes those rows.

    By the time anyone downgrades, real roles reference them through `role_permissions`,
    so the delete is only safe because that foreign key cascades. If it did not, a
    rollback would abort halfway on a foreign-key violation — in the one situation where
    everything is already going badly. The cascade is asserted here rather than assumed
    from reading the DDL.
    """
    url = _url(own_container)
    _alembic("upgrade head", url)

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO tenants (name) VALUES ('Downgrade')"))
        conn.execute(
            text(
                "INSERT INTO roles (tenant_id, name) "
                "SELECT id, 'referencing' FROM tenants WHERE name = 'Downgrade'"
            )
        )
        conn.execute(
            text(
                "INSERT INTO role_permissions (role_id, permission_code) "
                "SELECT id, 'query.execute' FROM roles WHERE name = 'referencing'"
            )
        )

    _alembic("downgrade 0001", url)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM permissions")).scalar() == 0
        # The grant went with it rather than blocking the rollback.
        assert conn.execute(text("SELECT count(*) FROM role_permissions")).scalar() == 0

    _alembic("upgrade head", url)
    engine.dispose()


def test_schema_matches_models(own_container: PostgresContainer) -> None:
    """Models and database describe the same thing.

    This is the test that prevents Alembic's most expensive failure: someone changes
    a model, forgets to generate a migration, and the gap only shows up in production.
    """
    url = _url(own_container)
    _alembic("upgrade head", url)

    engine = create_engine(url)
    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"include_object": _ignore_external})
        differences = compare_metadata(context, Base.metadata)
    engine.dispose()

    assert differences == [], f"schema has drifted from the models: {differences}"


def _ignore_external(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    return not (type_ == "table" and name == "spatial_ref_sys")


def _table_count(conn: Connection) -> int:
    """Counts project tables only: `alembic_version` belongs to the tool and
    `spatial_ref_sys` is created by PostGIS inside the ParadeDB image."""
    result = conn.execute(
        text(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename NOT IN ('alembic_version', 'spatial_ref_sys')"
        )
    ).scalar()
    return result or 0
