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

**Both shapes, because migration 0026 ships the one the LIST probe does not exercise.**
`chunks` and `chunk_embeddings` are partitioned `BY HASH (tenant_id)`, so a bucket holds a
residue class of tenants rather than one named tenant, and there is no `FOR VALUES IN` in
the schema at all. A guard proven only against LIST would be a guard proven against a shape
this installation does not have. The hash probe below is the same experiment with the same
two directions — a bare bucket that leaks, helper-made buckets that do not — and one extra
assertion the LIST probe cannot make: that the two shapes carry the *same policy expression*,
read back from `pg_policy` rather than from the constant both were built from. Comparing the
constant to itself is the failure mode `85d2174` is in this repository for.

The hash probe does not name its buckets by tenant, because it cannot: which tenant lands in
which bucket is `hashuuidextended`'s business. It asks `satisfies_hash_partition` instead, so
the bucket chosen to be bare is the one that provably holds the *other* tenant's rows and the
leak the test then measures is a real one rather than an empty table returning nothing.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.partitions import (
    LABEL_POLICY,
    hash_partition_statements,
    partition_statements,
)

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

#: Every hash bucket in the schema, with the modulus and remainder it declares. Read out of
#: `relpartbound` rather than out of the migration, because the question is what the
#: database has and not what a file says it should.
_HASH_BOUNDS = """
    SELECT root.relname,
           (regexp_match(pg_get_expr(child.relpartbound, child.oid),
                         'MODULUS (\\d+), REMAINDER (\\d+)'))[1]::int,
           (regexp_match(pg_get_expr(child.relpartbound, child.oid),
                         'MODULUS (\\d+), REMAINDER (\\d+)'))[2]::int
    FROM pg_class child
    JOIN pg_namespace n ON n.oid = child.relnamespace
    JOIN pg_class root ON root.oid = pg_partition_root(child.oid)
    WHERE n.nspname = :schema
      AND child.relispartition
      AND pg_get_expr(child.relpartbound, child.oid) LIKE 'FOR VALUES WITH (MODULUS%'
"""

#: The policy expression as Postgres stores it, per relation. Both the `USING` and the
#: `WITH CHECK` half: a partition given the right `USING` and a weaker `WITH CHECK` reads
#: correctly and accepts a row belonging to somebody else.
_POLICY_TEXT = """
    SELECT c.relname,
           pg_get_expr(p.polqual, p.polrelid),
           pg_get_expr(p.polwithcheck, p.polrelid)
    FROM pg_policy p
    JOIN pg_class c ON c.oid = p.polrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = :schema
    ORDER BY c.relname
"""

PROBE_SCHEMA = "partition_guard_probe"
HASH_PROBE_SCHEMA = "partition_guard_hash_probe"

#: Modulus 4 rather than 0026's 256. What is being proved is that a bucket carries its own
#: policy and that a bare one leaks, and neither is a function of how many buckets there
#: are; 256 empty Tantivy and HNSW directories per test run is a cost with no assertion
#: behind it.
HASH_MODULUS = 4

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


async def test_every_hash_partitioned_table_covers_every_remainder(
    owner_engine: AsyncEngine,
) -> None:
    """A residue class with no partition is a write path that fails at runtime.

    Postgres accepts an incomplete hash set at `CREATE TABLE` time and refuses the row that
    lands in the gap — `no partition of relation "chunks" found for row` — so an interrupted
    repartition leaves a schema that reads as finished, serves every read correctly, and
    breaks ingestion for the fraction of tenants that hash into the missing buckets. That is
    the shape of failure this repository keeps writing tests about: correct where anybody
    looks, wrong somewhere nobody does.

    The expectation is derived from the modulus each partition declares, not from a number
    written here. A guard whose bar is a literal is a guard that has to be edited every time
    0026's modulus changes, and the edit is where it stops being true.
    """
    async with owner_engine.connect() as connection:
        rows = await connection.execute(text(_HASH_BOUNDS), {"schema": "public"})
        buckets: dict[str, set[int]] = {}
        moduli: dict[str, set[int]] = {}
        for parent, modulus, remainder in rows:
            buckets.setdefault(parent, set()).add(remainder)
            moduli.setdefault(parent, set()).add(modulus)

    incomplete = {
        parent: sorted(set(range(max(moduli[parent]))) - remainders)
        for parent, remainders in buckets.items()
        if moduli[parent] != {len(remainders)} or remainders != set(range(len(remainders)))
    }

    assert incomplete == {}, (
        "hash-partitioned tables with residue classes no partition accepts: "
        + ", ".join(
            f"{parent} is missing remainders {missing} of modulus {sorted(moduli[parent])}"
            for parent, missing in incomplete.items()
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


@dataclass(frozen=True, slots=True)
class HashProbe:
    """The hash probe, and the bucket names the test cannot know before it runs."""

    connection: AsyncConnection
    #: The bucket the context tenant's rows land in, made by the helper.
    own: str
    #: The bucket another tenant's rows land in, made by hand and left bare.
    bare: str
    #: A third bucket, made by the helper, holding a third tenant.
    other: str
    #: The tenant whose context the leak test sets.
    context_tenant: str


async def _bucket_of(connection: AsyncConnection, parent: str, tenant: str) -> int:
    """Which remainder `tenant` hashes into, asked of Postgres rather than reimplemented.

    `satisfies_hash_partition` is the same function the partition constraint uses, so this
    cannot disagree with where the row actually goes — which a Python reimplementation of
    `hashuuidextended` could, silently, and only for some UUIDs.
    """
    remainder = await connection.scalar(
        text(
            "SELECT r FROM generate_series(0, :top) r WHERE satisfies_hash_partition("
            "CAST(:parent AS regclass), :modulus, r, CAST(:tenant AS uuid))"
        ),
        {"top": HASH_MODULUS - 1, "parent": parent, "modulus": HASH_MODULUS, "tenant": tenant},
    )
    assert remainder is not None, f"{tenant} lands in no bucket of modulus {HASH_MODULUS}"
    return int(remainder)


def _three_distinct(buckets: dict[str, int]) -> tuple[str, str, str] | None:
    """Three tenants in three different buckets, or nothing.

    Which tenants those are is `hashuuidextended`'s decision. Picking them at runtime rather
    than hard-coding three UUIDs known to separate today is the difference between a test
    that keeps testing what it says and one that quietly starts asserting that an empty
    table returns no rows.
    """
    seen: dict[int, str] = {}
    for tenant, bucket in buckets.items():
        seen.setdefault(bucket, tenant)
    if len(seen) < 3:
        return None
    chosen = [seen[bucket] for bucket in sorted(seen)][:3]
    return chosen[0], chosen[1], chosen[2]


#: Candidate tenants for the hash probe. Sixteen sequential UUIDs, of which three that land
#: in three different buckets are used; the surplus exists because four buckets and three
#: tenants collide often enough that a fixed three would be flaky.
CANDIDATE_TENANTS = [f"00000000-0000-0000-0000-{index:012d}" for index in range(1, 17)]


@pytest.fixture
async def hash_probe(owner_engine: AsyncEngine) -> AsyncIterator[HashProbe]:
    """The same experiment as `probe`, in the shape migration 0026 actually ships.

    Rolled back rather than dropped, for the same reason: the rollback is the drop and it
    happens whatever the test does.
    """
    async with owner_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            yield await _build_hash_probe(connection)
        finally:
            await transaction.rollback()


async def _build_hash_probe(connection: AsyncConnection) -> HashProbe:
    parent = f"{HASH_PROBE_SCHEMA}.chunks"
    for statement in (
        f"CREATE SCHEMA {HASH_PROBE_SCHEMA}",
        f"CREATE TABLE {parent} ("
        "  tenant_id uuid NOT NULL,"
        "  label_ids uuid[] NOT NULL DEFAULT ARRAY[]::uuid[],"
        "  text text"
        ") PARTITION BY HASH (tenant_id)",
        f"ALTER TABLE {parent} ENABLE ROW LEVEL SECURITY",
        f"CREATE POLICY chunks_isolation ON {parent} "
        f"USING ({LABEL_POLICY}) WITH CHECK ({LABEL_POLICY})",
    ):
        await connection.execute(text(statement))

    buckets = {tenant: await _bucket_of(connection, parent, tenant) for tenant in CANDIDATE_TENANTS}
    distinct = _three_distinct(buckets)
    assert distinct is not None, (
        f"no three of {len(CANDIDATE_TENANTS)} candidate tenants separate across "
        f"{HASH_MODULUS} buckets — the probe would be asserting about empty tables"
    )
    own_tenant, bare_tenant, other_tenant = distinct
    names = {
        buckets[own_tenant]: "bucket_own",
        buckets[bare_tenant]: "bucket_bare",
        buckets[other_tenant]: "bucket_other",
    }

    for remainder in range(HASH_MODULUS):
        name = f"{HASH_PROBE_SCHEMA}.{names.get(remainder, f'bucket_spare_{remainder}')}"
        if remainder == buckets[bare_tenant]:
            # The mistake, in one statement: a bucket and nothing else. This is what
            # `CREATE TABLE ... PARTITION OF` looks like when somebody assumes the parent's
            # row-level security reaches it.
            await connection.execute(
                text(
                    f"CREATE TABLE {name} PARTITION OF {parent} "
                    f"FOR VALUES WITH (MODULUS {HASH_MODULUS}, REMAINDER {remainder})"
                )
            )
            continue
        for statement in hash_partition_statements(parent, name, HASH_MODULUS, remainder):
            await connection.execute(text(statement))

    await connection.execute(
        text(
            f"INSERT INTO {parent} (tenant_id, text) "
            "SELECT t, 'row' FROM unnest(:tenants) t, generate_series(1, 3)"
        ),
        {"tenants": [UUID(own_tenant), UUID(bare_tenant), UUID(other_tenant)]},
    )
    return HashProbe(
        connection=connection,
        own="bucket_own",
        bare="bucket_bare",
        other="bucket_other",
        context_tenant=own_tenant,
    )


async def test_the_check_reports_a_bare_hash_bucket_and_clears_the_helper_made_ones(
    hash_probe: HashProbe,
) -> None:
    """The enumeration test, driven against the shape 0026 ships.

    Every bucket is reported, spares included: the guard walks partitions and not a sample,
    and a spare bucket that had lost its policy would be exactly the partition an
    installation never notices because no tenant is in it *yet*.
    """
    reported = {
        partition.name: partition.is_guarded
        for partition in await _relations(hash_probe.connection, _PARTITIONS, HASH_PROBE_SCHEMA)
    }

    assert len(reported) == HASH_MODULUS, f"the guard saw {len(reported)} of {HASH_MODULUS}"
    assert reported[hash_probe.bare] is False
    assert all(guarded for name, guarded in reported.items() if name != hash_probe.bare), (
        f"a helper-made bucket came back unguarded: {reported}"
    )


async def test_the_bare_hash_bucket_leaks_and_the_helper_made_ones_do_not(
    hash_probe: HashProbe,
) -> None:
    """What the catalogue difference costs in a hash bucket, read as `zenith_app`.

    Same two directions as the LIST probe. The bare bucket holds another tenant's three rows
    and hands all three over; the helper-made bucket holding a third tenant returns nothing;
    the helper-made bucket holding the context tenant returns its rows, which is the
    direction a policy that hides everything from everybody would pass without.
    """
    connection = hash_probe.connection
    for statement in (
        f"GRANT USAGE ON SCHEMA {HASH_PROBE_SCHEMA} TO zenith_app",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA {HASH_PROBE_SCHEMA} TO zenith_app",
        "SET LOCAL ROLE zenith_app",
        f"SELECT set_config('zenith.tenant_id', '{hash_probe.context_tenant}', true)",
        "SELECT set_config('zenith.label_ids', '', true)",
    ):
        await connection.execute(text(statement))

    visible: dict[str, int] = {}
    for bucket in (hash_probe.bare, hash_probe.other, hash_probe.own):
        rows = await connection.execute(text(f"SELECT count(*) FROM {HASH_PROBE_SCHEMA}.{bucket}"))
        visible[bucket] = rows.scalar_one()

    assert visible == {hash_probe.bare: 3, hash_probe.other: 0, hash_probe.own: 3}


async def test_both_shapes_carry_the_same_policy_expression(
    probe: AsyncConnection, hash_probe: HashProbe
) -> None:
    """Read back out of `pg_policy`, not compared against the constant that built it.

    `LABEL_POLICY` is one string and both helpers interpolate it, so comparing either
    partition's policy to `LABEL_POLICY` proves only that Python can concatenate. What has to
    hold is that Postgres parsed and stored the same expression for a LIST partition, a HASH
    bucket and the parent both hang off — the same class of check as `85d2174`, where a
    report read 1.0000 because it was comparing something against itself.

    `WITH CHECK` as well as `USING`: a bucket given the right `USING` and a weaker `WITH
    CHECK` reads correctly and accepts a row belonging to somebody else, and `INSERT` is the
    direction nothing else here exercises.
    """
    list_policies = {
        name: (using, check)
        for name, using, check in await probe.execute(text(_POLICY_TEXT), {"schema": PROBE_SCHEMA})
    }
    hash_policies = {
        name: (using, check)
        for name, using, check in await hash_probe.connection.execute(
            text(_POLICY_TEXT), {"schema": HASH_PROBE_SCHEMA}
        )
    }

    expressions = set(list_policies.values()) | set(hash_policies.values())

    assert len(expressions) == 1, (
        "the two partition shapes do not carry the same policy expression: "
        f"list={list_policies}, hash={hash_policies}"
    )
    using, check = expressions.pop()
    assert check == using, f"WITH CHECK differs from USING: {check!r} against {using!r}"
