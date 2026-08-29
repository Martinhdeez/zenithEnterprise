"""A partition does not inherit its parent's row-level security. This is what notices.

Partitioning `chunks` and `chunk_embeddings` by tenant is the structural route to the corpus
sizes this product is aimed at: it is the only lever that reduces how many vectors a query
searches rather than how many bytes each one costs, and therefore the only one that touches
the HNSW graph's own recall degradation. Before any of that is worth building, one thing has
to hold — RLS must still be the only access control. It does not hold by itself.

`eval/partition-rls.sql` measured it against the live database rather than assuming it:

    relname | rls_enabled | own_policies
    chunks  | t           | 1
    p2      | f           | 0

`ENABLE ROW LEVEL SECURITY` on the parent, and this project's own policy template on the
parent, pass neither to the partitions. With `zenith.tenant_id` set to tenant 1, a query
against the parent correctly returns tenant 1's ten thousand rows; a query against partition
`p2` returns tenant 3's ten thousand rows in full. A partition is an ordinary table with its
own grants and its own — absent — policies.

Reaching those rows needs a `GRANT` on the partition, and nothing grants one today. But
`GRANT ... ON ALL TABLES IN SCHEMA public TO zenith_app` would, and migration 0001 issues
exactly that statement; so would any migration written by somebody who assumed inheritance,
which is the assumption this file exists to kill. It is invariant 1 broken by a schema
change, and it fails in the direction this repository refuses: the ordinary path through the
parent keeps returning the right rows, so nothing surfaces.

So the schema is asked, in the shape of `test_security_definer_audit.py`: a convention
somebody has to remember is worth nothing, and a catalogue query is worth something.
`app.core.partitions.create_partition` is how a partition is meant to be made — table,
`ENABLE ROW LEVEL SECURITY` and policy in one operation, inside the migration's own
transaction — but nothing forces its use, and this is what catches a partition made without
it.

**Which database is the same limitation as the SECURITY DEFINER audit's.** This runs against
the testcontainers fixture, built from zero by the migrations, so what it guards is the
schema as *declared*. A partition created by hand on a running installation is invisible
here. `eval/partition-rls.sql` is the probe to run against an installation; a check inside
`zenith diagnose`, the other half of that split, belongs with the stage that actually
partitions something.

**The assertions against `public` pass vacuously today, because nothing is partitioned yet,
and a guard that has never failed is not a guard.** This repository carries `b20dde4`,
which exists because a check was measuring the wrong thing while looking healthy, and
`85d2174`, where a report read a perfect 1.0000 because it was comparing exact retrieval
against itself. So the last two tests build a partitioned table with a bare partition in a
schema of their own, prove the check reports it and prove the rows really do come back to
the wrong tenant, then prove the helper closes both. The transaction is rolled back, so the
probe schema never exists outside it.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.partitions import LABEL_POLICY, partition_statements

pytestmark = pytest.mark.asyncio


@dataclass(frozen=True, slots=True)
class Relation:
    """One partitioned relation as the catalogue actually holds it."""

    parent: str
    name: str
    rls_enabled: bool
    own_policies: int

    @property
    def is_guarded(self) -> bool:
        """Both halves, because either alone is useless.

        RLS enabled with no policy of its own returns nothing to anyone but the owner, which
        is the closed failure and not a leak. A policy with RLS disabled is a row in
        `pg_policy` that is never consulted, which is a leak that reads as protection in
        review. Neither state is one somebody meant to create.
        """
        return self.rls_enabled and self.own_policies > 0


#: Every partition whose partitioning *root* has RLS. The root rather than the direct
#: parent, so a partition of a partition is covered by the same rule; the root's RLS rather
#: than none at all, so that partitioning `permissions` or `embedding_spaces` — the two
#: catalogues 0001 deliberately leaves without RLS — would not fail for holding no policy.
_PARTITIONS = """
    SELECT root.relname,
           child.relname,
           child.relrowsecurity,
           (SELECT count(*) FROM pg_policy p WHERE p.polrelid = child.oid)
    FROM pg_class child
    JOIN pg_namespace n ON n.oid = child.relnamespace
    JOIN pg_class root ON root.oid = pg_partition_root(child.oid)
    WHERE n.nspname = :schema AND child.relispartition AND root.relrowsecurity
    ORDER BY root.relname, child.relname
"""

#: The partitioned tables themselves. A parent carrying no policy would make every
#: assertion above trivially true, since the query only follows roots that have one.
_PARENTS = """
    SELECT c.relname,
           c.relname,
           c.relrowsecurity,
           (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid)
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = :schema AND c.relkind = 'p' AND NOT c.relispartition
    ORDER BY c.relname
"""

PROBE_SCHEMA = "partition_guard_probe"

TENANT_ONE = "00000000-0000-0000-0000-000000000001"
TENANT_TWO = "00000000-0000-0000-0000-000000000002"
TENANT_THREE = "00000000-0000-0000-0000-000000000003"


async def _relations(connection: AsyncConnection, query: str, schema: str) -> list[Relation]:
    rows = await connection.execute(text(query), {"schema": schema})
    return [
        Relation(parent, name, rls_enabled, own_policies)
        for parent, name, rls_enabled, own_policies in rows
    ]


async def test_every_partition_of_an_rls_table_carries_its_own_policy(
    owner_engine: AsyncEngine,
) -> None:
    """The assertion the whole file is for.

    The owner connection is used on purpose: the catalogue has to be read by something that
    sees every relation, and a partition nobody granted `zenith_app` access to is exactly
    the one that would be missed by asking as `zenith_app`.
    """
    async with owner_engine.connect() as connection:
        partitions = await _relations(connection, _PARTITIONS, "public")

    unguarded = [partition for partition in partitions if not partition.is_guarded]

    assert unguarded == [], (
        "partitions of an RLS-protected table with no protection of their own: "
        + ", ".join(
            f"{partition.parent}/{partition.name} "
            f"(rls={partition.rls_enabled}, policies={partition.own_policies})"
            for partition in unguarded
        )
        + " — see app.core.partitions.create_partition"
    )


async def test_every_partitioned_table_carries_it_too(owner_engine: AsyncEngine) -> None:
    """The other half, so the test above cannot pass by asking about nothing.

    It follows roots that have RLS. A parent that lost its own policy would empty that
    query and leave every partition unexamined, which is the failure that looks most like
    success.
    """
    async with owner_engine.connect() as connection:
        parents = await _relations(connection, _PARENTS, "public")

    unguarded = [parent for parent in parents if not parent.is_guarded]

    assert unguarded == [], (
        "partitioned tables without row-level security of their own: "
        + ", ".join(
            f"{parent.name} (rls={parent.rls_enabled}, policies={parent.own_policies})"
            for parent in unguarded
        )
    )


@pytest.fixture
async def probe(owner_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A partitioned table with one bare partition and two made by the helper.

    Its own schema, and rolled back rather than dropped: DDL is transactional in Postgres,
    so the rollback is the drop and it happens whatever the test does. Nothing here can
    survive into `public` or outlast the test.
    """
    async with owner_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await _build_probe(connection)
            yield connection
        finally:
            await transaction.rollback()


async def _build_probe(connection: AsyncConnection) -> None:
    statements = [
        f"CREATE SCHEMA {PROBE_SCHEMA}",
        f"CREATE TABLE {PROBE_SCHEMA}.chunks ("
        "  tenant_id uuid NOT NULL,"
        "  label_ids uuid[] NOT NULL DEFAULT ARRAY[]::uuid[],"
        "  text text"
        ") PARTITION BY LIST (tenant_id)",
        # The parent, protected exactly as migration 0001 protects `chunks`. This is the
        # whole of what somebody assuming inheritance would write.
        f"ALTER TABLE {PROBE_SCHEMA}.chunks ENABLE ROW LEVEL SECURITY",
        f"CREATE POLICY chunks_isolation ON {PROBE_SCHEMA}.chunks "
        f"USING ({LABEL_POLICY}) WITH CHECK ({LABEL_POLICY})",
        # The mistake, in one statement: a partition and nothing else.
        f"CREATE TABLE {PROBE_SCHEMA}.bare PARTITION OF {PROBE_SCHEMA}.chunks "
        f"FOR VALUES IN ('{TENANT_TWO}')",
        *partition_statements(
            f"{PROBE_SCHEMA}.chunks", f"{PROBE_SCHEMA}.guarded_other", TENANT_THREE
        ),
        *partition_statements(f"{PROBE_SCHEMA}.chunks", f"{PROBE_SCHEMA}.guarded_own", TENANT_ONE),
        f"INSERT INTO {PROBE_SCHEMA}.chunks (tenant_id, text) "
        f"SELECT t, 'row' FROM unnest(ARRAY['{TENANT_ONE}', '{TENANT_TWO}', "
        f"'{TENANT_THREE}']::uuid[]) t, generate_series(1, 3)",
    ]
    for statement in statements:
        await connection.execute(text(statement))


async def test_the_check_reports_a_bare_partition_and_clears_the_helper_made_ones(
    probe: AsyncConnection,
) -> None:
    """The proof that the assertions above can fail.

    Both directions, for the same reason as the SECURITY DEFINER audit: a check that only
    ever reports a problem is as untrustworthy as one that never does. `bare` must come back
    unguarded and the two the helper built must come back guarded, from one query.
    """
    reported = {
        partition.name: partition.is_guarded
        for partition in await _relations(probe, _PARTITIONS, PROBE_SCHEMA)
    }

    assert reported == {"bare": False, "guarded_other": True, "guarded_own": True}


async def test_the_bare_partition_leaks_and_the_helper_made_ones_do_not(
    probe: AsyncConnection,
) -> None:
    """What the catalogue difference actually costs, read as `zenith_app`.

    The grants are the ones a careless migration makes — 0001 already grants `SELECT` on
    every table in `public` — and the context is tenant 1 throughout. `bare` holds tenant 2
    and hands all of it over. `guarded_other` holds tenant 3 and returns nothing.
    `guarded_own` holds tenant 1 and returns its rows, which is the direction that has to be
    checked too: a policy that hides everything from everybody would pass the test above and
    break the product.
    """
    for statement in (
        f"GRANT USAGE ON SCHEMA {PROBE_SCHEMA} TO zenith_app",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA {PROBE_SCHEMA} TO zenith_app",
        "SET LOCAL ROLE zenith_app",
        f"SELECT set_config('zenith.tenant_id', '{TENANT_ONE}', true)",
        "SELECT set_config('zenith.label_ids', '', true)",
    ):
        await probe.execute(text(statement))

    visible: dict[str, int] = {}
    for partition in ("bare", "guarded_other", "guarded_own"):
        rows = await probe.execute(text(f"SELECT count(*) FROM {PROBE_SCHEMA}.{partition}"))
        visible[partition] = rows.scalar_one()

    assert visible == {"bare": 3, "guarded_other": 0, "guarded_own": 3}
