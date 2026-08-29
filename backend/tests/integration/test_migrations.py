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
from conftest import BACKEND_DIR, IMAGE, MAX_LOCKS_PER_TRANSACTION


@pytest.fixture(scope="module")
def own_container() -> Iterator[PostgresContainer]:
    """A separate container: this module runs `downgrade base`, which would wipe the
    data the RLS tests rely on if it shared an instance.

    Same lock table as the session container and as the deployment — see
    `conftest.MAX_LOCKS_PER_TRANSACTION`. This is the module that runs 0026's `downgrade`,
    which is the transaction that needs it most.
    """
    with PostgresContainer(IMAGE, driver="psycopg").with_command(
        f"postgres -c max_locks_per_transaction={MAX_LOCKS_PER_TRANSACTION}"
    ) as container:
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


#: Reflected tables the models do not describe and should not be asked to.
#:
#: `spatial_ref_sys` is created by PostGIS inside the ParadeDB image. The partition prefixes
#: are migration 0026's 512 buckets: a partition is an ordinary table in `pg_class`, so
#: autogenerate reflects every one of them and proposes dropping it. Declaring 512 tables in
#: the models to silence that would be describing the same thing twice and would make the
#: modulus a number that has to be edited in two places to change.
#:
#: Matched by prefix rather than by `relispartition`, because `compare_metadata` hands this
#: hook a name and not a catalogue row. The prefixes are anchored to a partition-shaped
#: suffix so an ordinary table called `chunks_summary` would still be compared.
IGNORED_TABLES = ("spatial_ref_sys",)
PARTITION_PREFIXES = ("chunks_p", "chunk_embeddings_p")


def _is_partition(name: str | None) -> bool:
    return name is not None and any(
        name.startswith(prefix) and name[len(prefix) :].isdigit() for prefix in PARTITION_PREFIXES
    )


def _ignore_external(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Everything the models are not the authority on.

    Two categories, and the second is not obvious. A foreign key that *references* a
    partitioned table is expanded by Postgres into one constraint per referenced partition —
    `chunk_embeddings_chunk_id_tenant_id_fkey018` beside `fk_chunk_embeddings_chunk_id` — and
    reflection returns all 257 of them. They are one declared key, so the model declares one;
    the 256 are Postgres's own bookkeeping and proposing to drop them is the tool
    misunderstanding the schema rather than the schema having drifted.

    The declared parent key is *not* filtered, so this stays able to notice a foreign key
    that really has gone missing — which is exactly what it did notice: `LIKE` copies no
    foreign keys, and three were absent from 0026's first draft.
    """
    if name in IGNORED_TABLES:
        return False
    if type_ == "table":
        return not _is_partition(name)
    if type_ == "foreign_key_constraint":
        elements = getattr(obj, "elements", ())
        return not any(_is_partition(element.target_fullname.split(".")[0]) for element in elements)
    return True


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
