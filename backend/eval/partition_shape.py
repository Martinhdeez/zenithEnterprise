# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""How many partitions Postgres will carry, and which tenant the partitioning is for.

`eval/partition-pruning.sql` settled the question that had to be settled first: pruning
works when the key arrives from `zenith_current_tenant()`, which is `STABLE` and therefore
cannot prune at *plan* time. Eight partitions, `Subplans Removed: 7`, in three shapes
including a `PREPARE`/`EXECUTE` pair and the full policy with the label overlap. Partitioning
by tenant is available to this product.

Available is not the same as shaped. Two things decide the shape and neither is known:

**1. How many partitions before Postgres objects.** A partition per tenant is the clean
model and it is the one that does not survive ten thousand customers. The literature puts the
knee "in the thousands" without saying where, and where is a property of this workload —
this policy, this vector `ORDER BY`, this `max_locks_per_transaction`. So it is measured on a
ladder: 10, 100, 1,000, 2,000, 5,000, 10,000 partitions, in both candidate shapes, `LIST`
and `HASH`, recording planning time separately from execution time, `Subplans Removed` read
off `EXPLAIN` rather than assumed, and **the number of locks one query takes**, because that
is the pressure that appears under concurrency rather than in a single timing.

**2. Who the benefit is for.** Every sizing figure in the partitioning plan divides 322M
passages by 200 tenants and calls the result 1.6M. That average has never been measured and
it is not the number that bounds anything. If one tenant holds a third of the corpus, that
tenant searches a third of the graph and the benefit evaporates for exactly the customer who
weighs most. This sweep reports the distribution that actually exists, states the shape the
plan should assume instead of uniformity, and gives the benefit **parametrically in the
largest tenant's share** rather than as a headline percentage that is true only if every
customer is the same size.

## What is built, and why it is not the whole schema

Two partitioned tables, because the query that ships is a join and a join of two partitioned
relations is not the planning cost of one. `dense()` in `app/features/retrieval/search.py`
reads `chunk_embeddings` for the vector order and joins `chunks` for the label half of the
policy — that join is a security control, not a convenience, so a measurement that drops it
measures a query this product must never run. Both copies carry the real policy text.

The copies are deliberately *lightly* indexed: one HNSW index per embeddings partition and
one btree per chunks partition. The installation's `chunks` carries five indexes. Locks scale
with the number of *relations* a query touches, indexes included, so the measured lock count
is a floor and the report projects the production index set from it rather than pretending
the floor is the answer.

Rows are loaded into a fixed handful of tenants and every partition beyond them is left
empty. This is not a shortcut: **planning time and lock count do not depend on what a
partition contains**, and holding the queried partition's row count constant across the
ladder is what makes the execution times comparable between rungs at all. What varies up the
ladder is the partition count and nothing else.

**Read-only with respect to the corpus.** `chunks` and `chunk_embeddings` are read; the only
writes are into `zenith_partshape`, which is dropped in a `finally` and verified gone,
alongside the count of `SECURITY DEFINER` functions in `public`.

    docker compose exec -T api python -m eval partition-shape [--counts 10,100,1000]
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.core.config import settings
from app.core.hardware import active as active_profile
from app.features.retrieval.search import CANDIDATES, ITERATIVE_SCAN

REPORT = Path(__file__).parent / "partition-shape.json"

#: One schema, one name, dropped in a `finally`. Owned by this sweep and by nothing else: a
#: guard test is being written against this same database from another worktree and the only
#: thing keeping the two out of each other's way is that neither invents a second scratch
#: name.
SCHEMA = "zenith_partshape"

#: The ladder. 5,000 and 10,000 are on it because the interesting result is where it stops,
#: and a ladder that stops before the failure reports the ladder rather than the machine.
COUNTS: tuple[int, ...] = (10, 100, 1_000, 2_000, 5_000, 10_000)

#: `LIST` is one partition per tenant — the clean model, and the one whose partition count is
#: the customer count. `HASH` bounds the partition count independently of how many customers
#: there are, and what it costs is measured in `_hash_sharing` rather than asserted.
SHAPES: tuple[str, ...] = ("list", "hash")

#: Tenants that actually hold rows. Eight, as in `partition-pruning.sql`, so "one partition
#: scanned" and "all partitions scanned" are far apart in any plan and the queried partition
#: holds enough vectors that the planner still has a choice about how to read it.
POPULATED = 8

#: Partitions created per transaction during the build. Every `CREATE TABLE ... PARTITION OF`
#: takes locks that are held to the end of its transaction, so an unbatched build runs out of
#: the same lock table this sweep is measuring — and fails during setup, which would look
#: like a result and is not one.
BATCH = 200

#: Timed repeats per arm. The fastest is not kept here, unlike `eval/quantisation.py`: the
#: distribution is the point, because the claim being tested is about a cost that is paid
#: once per query and therefore shows up in the median rather than in the floor.
REPEATS = 7

#: What the dense half asks for. Imported rather than repeated.
WANTED = CANDIDATES

#: Executions of each prepared statement. Eight, because Postgres plans custom-ly five times
#: before it will consider a generic plan, and a run that stopped at five would report the
#: threshold rather than what happens past it.
PREPARED_RUNS = 8

#: Tenant counts to model the size distribution over. 200 is the number the partitioning plan
#: assumes; the others bracket it, because the plan's arithmetic changes shape with T and the
#: reader should be able to see which way.
MODELLED_TENANTS: tuple[int, ...] = (50, 200, 1_000, 10_000)

#: Zipf exponents to model tenant share with. There is **no customer data behind these** —
#: see `_tail`. They are candidate shapes, quoted so a reader can substitute the real one.
ZIPF_EXPONENTS: tuple[float, ...] = (0.8, 1.0, 1.2)

#: Hash moduli worth considering for a bounded shape. Powers of two because a hash partition
#: count is doubled by splitting, and a modulus that is not a power of two makes that a
#: rewrite of every partition rather than of half of them.
HASH_MODULI: tuple[int, ...] = (64, 256, 1_024)

#: The corpus size the partitioning plan is written against, in passages.
PLAN_PASSAGES = 322_000_000


def _tenant(index: int) -> str:
    """Synthetic tenant ids, dense and ordered, so a partition name and a tenant agree."""
    return f"00000000-0000-0000-0000-{index:012d}"


# --- Reading a plan --------------------------------------------------------------------
#
# Every number below that describes a plan is read out of `EXPLAIN` output rather than
# inferred from a timing. `eval/scan_bound.py` makes the argument at length: a sweep that
# infers its plan cannot tell "the thing I was measuring did not happen" from "the thing I
# was measuring happened and cost nothing", and those mean opposite things.

_PLANNING = re.compile(r"Planning Time: ([\d.]+) ms")
_EXECUTION = re.compile(r"Execution Time: ([\d.]+) ms")
_REMOVED = re.compile(r"Subplans Removed: (\d+)")


def _read_plan(lines: list[str]) -> dict[str, object]:
    """What a partitioned plan has to state about itself.

    `Subplans Removed` is the fact that matters and it only appears under `ANALYZE`: pruning
    on a `STABLE` key happens at executor startup, so a plan that was never started cannot
    report it. `partition-pruning.sql` uses `ANALYZE` for the same reason.

    Collected as a **list**, one entry per `Append`. The join arm has two partitioned
    relations and therefore two of them, and a single number would silently report one
    relation's pruning as the query's. An empty list means the node was not present at all —
    which on a partitioned table means nothing was pruned, and is a different finding from a
    zero.
    """
    joined = "\n".join(lines)
    planning = _PLANNING.search(joined)
    execution = _EXECUTION.search(joined)
    removed = [int(match) for match in _REMOVED.findall(joined)]
    scans = [
        line.strip().removeprefix("->  ").removeprefix("Parallel ")
        for line in lines
        if "Scan" in line
    ]
    return {
        "planning_ms": float(planning.group(1)) if planning else None,
        "execution_ms": float(execution.group(1)) if execution else None,
        "subplans_removed": removed,
        "subplans_removed_total": sum(removed),
        "appends": len(removed),
        "scan_nodes": len(scans),
        # Whether the vector order came out of an index or out of a sort afterwards. On a
        # partition holding a couple of thousand rows the planner may reasonably prefer the
        # sort, and a report that did not say so would attribute the choice to partitioning.
        "index_ordered": any(s.startswith("Index Scan using") for s in scans)
        and not any("Sort" in line for line in lines),
        "leaf_scans": scans[:4],
    }


async def _explain(conn: AsyncConnection, statement: str, params: dict[str, object]) -> list[str]:
    rows = await conn.execute(
        text(f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY ON) {statement}"), params
    )
    return [str(row[0]) for row in rows]


async def _locks(conn: AsyncConnection) -> int:
    """Locks held by this backend right now.

    Counted inside the transaction that ran the query, because that is the only place the
    number exists: every one of these is released at commit. A partitioned query takes an
    `AccessShareLock` on every partition and on every index of every partition *at plan
    time*, before runtime pruning has removed anything — which is why this grows with the
    partition count even though the scan touches one partition.
    """
    return int(
        (
            await conn.execute(text("SELECT count(*) FROM pg_locks WHERE pid = pg_backend_pid()"))
        ).scalar_one()
    )


# --- The queries -----------------------------------------------------------------------


def _dense_statement(schema: str, joined: bool) -> str:
    """The dense half, as `app/features/retrieval/search.py` writes it.

    `joined` picks between the query that ships and the same query with the `chunks` join
    removed. Both are reported, and the pair is the answer to "how much of the planning cost
    is the second partitioned relation" — which is the difference between partitioning one
    table and partitioning the schema.

    The join is never dropped in production and this sweep is not suggesting it could be. It
    carries label isolation for the dense half, which the `chunk_embeddings` policy does not.
    """
    embeddings = f"{schema}.emb e"
    if not joined:
        return (
            f"SELECT e.chunk_id FROM {embeddings} "
            "WHERE e.embedding_model = :model AND e.embedding_version = :version "
            "ORDER BY e.embedding_half <=> CAST(:embedding AS halfvec(1024)) LIMIT :limit"
        )
    return (
        "SELECT c.id, 1 - (e.embedding_half <=> CAST(:embedding AS halfvec(1024))) AS score "
        f"FROM {embeddings} "
        f"JOIN {schema}.chk c ON c.id = e.chunk_id "
        "WHERE e.embedding_model = :model AND e.embedding_version = :version "
        "ORDER BY e.embedding_half <=> CAST(:embedding AS halfvec(1024)) LIMIT :limit"
    )


async def _context(conn: AsyncConnection, tenant: str, ef_search: int) -> None:
    """The session the application would have, set the way the application sets it.

    Four `set_config` calls in one target list and a `SET LOCAL statement_timeout`, exactly as
    `app.core.database.set_rls_context` does — except the timeout, which is raised well above
    the product's. Planning is inside `statement_timeout`, and at the top of this ladder
    planning alone can exceed the ten seconds the product allows. A rung that timed out would
    be reported as a failure to plan rather than as the planning cost it is.
    """
    await conn.execute(
        text(
            "SELECT set_config('zenith.tenant_id', :tenant, true), "
            "       set_config('zenith.label_ids', '', true), "
            "       set_config('zenith.user_id', '', true), "
            "       set_config('zenith.reads_all_history', 'false', true)"
        ),
        {"tenant": tenant},
    )
    await conn.execute(text("SET LOCAL statement_timeout = '300s'"))
    await conn.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))
    await conn.execute(text(f"SET LOCAL hnsw.iterative_scan = {ITERATIVE_SCAN}"))
    # A parallel plan over thousands of partitions asks for a shared memory segment larger
    # than the 1 GB this container has and fails with `DiskFull`, which names the wrong
    # resource. Serial here, as in `eval/scan_bound.py`, and for the same reason: laboratory
    # tooling should not require a change to how the product is deployed.
    await conn.execute(text("SET LOCAL max_parallel_workers_per_gather = 0"))


async def _arm(
    app: AsyncEngine, statement: str, params: dict[str, object], tenant: str, ef_search: int
) -> dict[str, object]:
    """One query shape at one rung: its plan, its locks and its latency.

    Everything happens in one transaction as `zenith_app`, so the policies apply and the lock
    count is the count for a single request. Failure is caught and recorded rather than
    raised: running out of the shared lock table *is* the measurement at the top of the
    ladder, and an exception there would throw away every rung below it.
    """
    async with app.connect() as conn:
        try:
            await _context(conn, tenant, ef_search)
            before = await _locks(conn)
            plan = _read_plan(await _explain(conn, statement, params))
            after = await _locks(conn)

            times: list[float] = []
            rows = 0
            for _ in range(REPEATS):
                started = time.perf_counter()
                result = await conn.execute(text(statement), params)
                rows = len(result.all())
                times.append((time.perf_counter() - started) * 1000)
            ordered = sorted(times)
            return {
                **plan,
                "locks_before": before,
                "locks_held": after,
                "locks_for_query": after - before,
                "returned": rows,
                "median_ms": round(statistics.median(times), 2),
                "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 2),
                "min_ms": round(min(times), 2),
            }
        except Exception as exc:  # noqa: BLE001 - the failure is the result at the top rungs
            return {"error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"}
        finally:
            await conn.rollback()


#: Relations per partition, as this ladder builds them and as the installation's schema would
#: have them. A lock is taken per relation, and an index is a relation: `chunk_embeddings`
#: carries a primary key beside its HNSW index, and `chunks` carries a primary key plus the
#: BM25, label, tenant and tsvector indexes. So the ladder's lock counts are a floor and the
#: multiplier out of it is arithmetic rather than another measurement.
BUILT_RELATIONS = {"embeddings": 2, "join": 4}
PRODUCTION_RELATIONS = {"embeddings": 3, "join": 9}


def _pressure(partitions: int, locks: int, slots: int, arm: str) -> dict[str, object]:
    """What the lock count means, which is not what a single query's timing suggests.

    `max_locks_per_transaction` does not cap a transaction. It sizes one table the whole
    cluster draws from — `max_locks_per_transaction * max_connections` entries — so a query
    that takes four thousand locks has not used four thousand of its own allowance, it has
    used two thirds of the installation's. The number that matters is therefore how many such
    queries can be in flight, and it is the one a single-query benchmark cannot see.

    The concurrency figures below divide by the *nominal* slot count and are conservative on
    purpose: see `_ceiling`, where the measured boundary turns out to be higher and to depend
    on shared memory nobody has reserved.
    """
    projected = round(locks * PRODUCTION_RELATIONS[arm] / BUILT_RELATIONS[arm])
    return {
        "locks_per_partition": round(locks / partitions, 2),
        "projected_locks_with_production_indexes": projected,
        "concurrent_queries_as_built": slots // locks if locks else None,
        "concurrent_queries_with_production_indexes": slots // projected if projected else None,
    }


def _ceiling(ladder: list[dict[str, object]], slots: int) -> dict[str, object]:
    """Where the ladder actually stopped, read off the ladder rather than predicted from it.

    The prediction and the observation disagree, and the disagreement is the finding. The
    nominal lock table is `max_locks_per_transaction * max_connections`, and a query here held
    ten thousand locks against a nominal six thousand four hundred without complaint —
    because Postgres allocates the lock hash table in shared memory and lets it grow into
    whatever slack the segment has. **That slack is not reserved and not yours.** It is
    whatever no other backend happened to want at that moment, so a rung that passes on an
    idle installation is not a rung that passes under load, and the number to plan against is
    the nominal one.

    Reported as a bracket — the largest lock count seen to succeed and the smallest seen to
    fail — because that is what was observed, and a single threshold would be a claim the
    measurement does not support.
    """
    succeeded: list[int] = []
    failed: list[int] = []
    for rung in ladder:
        arms = rung.get("arms")
        if not isinstance(arms, dict):
            continue
        for key, arm in arms.items():
            if not isinstance(arm, dict) or key not in BUILT_RELATIONS:
                continue
            locks = arm.get("locks_for_query")
            if isinstance(locks, int):
                succeeded.append(locks)
            elif "out of shared memory" in str(arm.get("error", "")):
                # The failed arm never reported a lock count, because it never got far enough
                # to be asked. What it would have taken is the slope the successful rungs
                # measured, and it is exact rather than fitted: relations per partition.
                failed.append(BUILT_RELATIONS[key] * int(str(rung["partitions"])))
    return {
        "nominal_slots": slots,
        "nominal_derivation": "max_locks_per_transaction * max_connections",
        "observed_highest_locks_that_succeeded": max(succeeded, default=None),
        "observed_lowest_locks_that_failed": min(failed, default=None),
        "observed_note": (
            "A query held more locks than the nominal table holds and still ran. The lock "
            "hash table grows into unreserved shared memory, so the surplus is real, "
            "transient and shared with every other backend. Plan against the nominal number."
        ),
        "failure_mode": (
            "psycopg.errors.OutOfMemory: out of shared memory, hinting at "
            "max_locks_per_transaction. It is raised during planning, so the query never "
            "runs and the request is a 500 rather than a slow answer."
        ),
        "safe_partitions_at_nominal_slots": {
            arm: {
                "one_query": slots // relations,
                "four_concurrent": slots // (relations * 4),
                "twenty_five_concurrent": slots // (relations * 25),
            }
            for arm, relations in PRODUCTION_RELATIONS.items()
        },
        "raising_it": (
            "max_locks_per_transaction requires a restart and costs shared memory linearly. "
            "P partitions at C concurrent joined queries needs about "
            "9 * P * C / max_connections, so 1,000 partitions at 25 concurrent requests on "
            "this installation's 100 connections would need roughly 2,250, from a default "
            "of 64."
        ),
    }


async def _prepared(
    app: AsyncEngine, statement: str, params: dict[str, object], tenant: str, ef_search: int
) -> dict[str, object]:
    """What a prepared statement costs, generic and custom, and whether it still prunes.

    The driver sends prepared statements, so this is not a curiosity. A generic plan is built
    once and reused, which would make the planning cost measured above a per-connection cost
    rather than a per-query one — if the generic plan is usable. Whether it is usable is
    precisely the question: a generic plan is built with no knowledge of the parameters, and
    the tenant does not arrive as a parameter anyway, it arrives from a `STABLE` function. So
    both modes are forced rather than left to `auto`, which would silently pick one and report
    a number without saying which question it answered.

    Eight executions each, and `auto` is measured beside the two forced modes because it is
    the one that ships. Postgres plans a prepared statement custom-ly five times and then
    compares: if the generic plan's estimate is no worse than the average custom cost *plus
    the planning it would save*, it switches. Whether that switch happens here is a question
    about a cost estimate on a five-thousand-partition Append, and guessing at it would be
    guessing at the only number in this file that decides what stage 02 has to configure. So
    it is run: eight executions is three past the threshold, and the planning-time column
    shows the switch happening or not happening.

    psycopg promotes a statement to a server-side prepared one after five executions of its
    own accord, so this path is not hypothetical for this product — it is what the pool does.
    """
    name = "partshape_probe"
    out: dict[str, object] = {}
    for mode in ("auto", "force_custom_plan", "force_generic_plan"):
        async with app.connect() as conn:
            try:
                await _context(conn, tenant, ef_search)
                await conn.execute(text(f"SET LOCAL plan_cache_mode = {mode}"))
                # `$1` rather than a named parameter: this is server-side `PREPARE`, not the
                # driver's. Inlining the other two literals keeps the statement to the one
                # parameter whose plan-time absence is being tested.
                prepared = (
                    statement.replace(":embedding", "$1")
                    .replace(":model", f"'{params['model']}'")
                    .replace(":version", f"'{params['version']}'")
                    .replace(":limit", str(params["limit"]))
                )
                await conn.execute(text(f"PREPARE {name} (text) AS {prepared}"))
                # The vector is inlined into the `EXECUTE` rather than bound. A driver-level
                # placeholder inside an `EXECUTE` argument list is a parameter to a statement
                # that is itself parameterised, and whether that plans as a generic or a
                # custom plan is exactly the thing under test — so the only parameter left in
                # play is the one `PREPARE` declared.
                runs: list[dict[str, object]] = []
                for _ in range(PREPARED_RUNS):
                    lines = await _explain(conn, f"EXECUTE {name}('{params['embedding']}')", {})
                    runs.append(_read_plan(lines))
                await conn.execute(text(f"DEALLOCATE {name}"))
                out[mode] = {
                    "planning_ms": [run["planning_ms"] for run in runs],
                    "execution_ms": [run["execution_ms"] for run in runs],
                    "subplans_removed": [run["subplans_removed"] for run in runs],
                    # The plan itself, not only its cost. "Does it still prune" and "is it
                    # still a good plan" are separate questions with separate answers, and
                    # the first run of this sweep returned yes to the first and a thirtyfold
                    # slowdown to the second. A report carrying only `subplans_removed` would
                    # have said the generic plan was fine.
                    "index_ordered": [run["index_ordered"] for run in runs],
                    "leaf_scans": runs[-1]["leaf_scans"],
                }
            except Exception as exc:  # noqa: BLE001 - same reason as `_arm`
                out[mode] = {"error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"}
            finally:
                await conn.rollback()
    return out


# --- Building and unbuilding -----------------------------------------------------------


async def _drop(owner: AsyncEngine) -> None:
    """Drop the scratch schema in batches, because `CASCADE` on ten thousand tables cannot.

    `DROP SCHEMA ... CASCADE` is one transaction and takes an `AccessExclusiveLock` on every
    object in it. At the top of this ladder that is more locks than the shared table holds,
    so the tidy-up fails and the schema survives — which is the one outcome this file is not
    allowed to produce. Children first, in batches, then the parents and the schema.
    """
    async with owner.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        while True:
            names = [
                str(row[0])
                for row in await conn.execute(
                    text(
                        "SELECT c.relname FROM pg_class c JOIN pg_namespace n "
                        "ON n.oid = c.relnamespace "
                        "WHERE n.nspname = :s AND c.relkind = 'r' LIMIT :n"
                    ),
                    {"s": SCHEMA, "n": BATCH},
                )
            ]
            if not names:
                break
            targets = ", ".join(f"{SCHEMA}.{name}" for name in names)
            await conn.execute(text(f"DROP TABLE IF EXISTS {targets} CASCADE"))
        await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))


_PARENT_DDL = """
CREATE TABLE {schema}.emb (
  chunk_id          uuid NOT NULL,
  tenant_id         uuid NOT NULL,
  embedding_model   varchar NOT NULL,
  embedding_version varchar NOT NULL,
  embedding_half    halfvec(1024) NOT NULL
) PARTITION BY {by};
CREATE TABLE {schema}.chk (
  id        uuid NOT NULL,
  tenant_id uuid NOT NULL,
  label_ids uuid[] NOT NULL DEFAULT '{{}}'
) PARTITION BY {by};
"""

#: The policies, copied from the installation rather than paraphrased. `chunk_embeddings` is
#: tenant-only because it sits on the hot path; `chunks` carries the label overlap. Getting
#: this pair wrong would measure a cheaper predicate than the one that ships.
_POLICY_DDL = """
ALTER TABLE {schema}.emb ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.chk ENABLE ROW LEVEL SECURITY;
CREATE POLICY emb_isolation ON {schema}.emb
  USING (tenant_id = zenith_current_tenant());
CREATE POLICY chk_isolation ON {schema}.chk
  USING (tenant_id = zenith_current_tenant()
         AND (label_ids = '{{}}' OR label_ids && zenith_current_labels()));
GRANT SELECT ON {schema}.emb, {schema}.chk TO zenith_app;
"""


async def _partitions(conn: AsyncConnection, shape: str, count: int) -> None:
    """Create `count` partitions of both tables, plus one index each, in batches.

    The index per partition is not decoration. Locks are taken per *relation*, so an index is
    a lock; and the planner builds paths for every index of every unpruned partition, so an
    index is planning time. A ladder built without indexes would understate both, and would
    understate them by a factor that depends on how many indexes the real table carries.
    """
    if shape == "list":
        bounds = (
            "format('FOR VALUES IN (%L)', "
            "('00000000-0000-0000-0000-' || lpad(i::text, 12, '0'))::uuid)"
        )
    else:
        bounds = f"format('FOR VALUES WITH (MODULUS {count}, REMAINDER %s)', i)"

    for low in range(0, count, BATCH):
        high = min(low + BATCH, count)
        await conn.execute(
            text(
                "DO $do$ DECLARE i int; BEGIN "
                f"FOR i IN {low}..{high - 1} LOOP "
                f"  EXECUTE 'CREATE TABLE {SCHEMA}.e' || i "
                f"       || ' PARTITION OF {SCHEMA}.emb ' || {bounds}; "
                f"  EXECUTE 'CREATE TABLE {SCHEMA}.c' || i "
                f"       || ' PARTITION OF {SCHEMA}.chk ' || {bounds}; "
                f"  EXECUTE 'CREATE INDEX ON {SCHEMA}.e' || i "
                "       || ' USING hnsw (embedding_half halfvec_cosine_ops) "
                "WITH (m = 16, ef_construction = 64)'; "
                f"  EXECUTE 'CREATE INDEX ON {SCHEMA}.c' || i || ' (id)'; "
                "END LOOP; END $do$;"
            )
        )


async def _flat(conn: AsyncConnection, space: tuple[str, str]) -> None:
    """The unpartitioned pair, built once, from the same assignment table.

    Without it every planning time on the ladder is a number with nothing to be compared to.
    This is the shape the installation runs today.
    """
    await conn.execute(
        text(
            f"CREATE TABLE {SCHEMA}.emb_flat AS "
            "SELECT a.chunk_id, a.tenant_id, e.embedding_model, e.embedding_version, "
            "       e.embedding_half "
            f"FROM {SCHEMA}.assign a JOIN chunk_embeddings e ON e.chunk_id = a.chunk_id "
            "WHERE e.embedding_model = :model AND e.embedding_version = :version"
        ),
        {"model": space[0], "version": space[1]},
    )
    await conn.execute(
        text(
            f"CREATE TABLE {SCHEMA}.chk_flat AS "
            "SELECT a.chunk_id AS id, a.tenant_id, '{}'::uuid[] AS label_ids "
            f"FROM {SCHEMA}.assign a"
        )
    )
    await conn.execute(
        text(f"ALTER TABLE {SCHEMA}.emb_flat ALTER COLUMN embedding_half SET NOT NULL")
    )
    await conn.execute(
        text(
            f"CREATE INDEX ON {SCHEMA}.emb_flat USING hnsw "
            "(embedding_half halfvec_cosine_ops) WITH (m = 16, ef_construction = 64)"
        )
    )
    await conn.execute(text(f"CREATE INDEX ON {SCHEMA}.chk_flat (id)"))
    await conn.execute(text(f"ALTER TABLE {SCHEMA}.emb_flat ENABLE ROW LEVEL SECURITY"))
    await conn.execute(text(f"ALTER TABLE {SCHEMA}.chk_flat ENABLE ROW LEVEL SECURITY"))
    await conn.execute(
        text(
            f"CREATE POLICY emb_flat_isolation ON {SCHEMA}.emb_flat "
            "USING (tenant_id = zenith_current_tenant())"
        )
    )
    await conn.execute(
        text(
            f"CREATE POLICY chk_flat_isolation ON {SCHEMA}.chk_flat "
            "USING (tenant_id = zenith_current_tenant() "
            "AND (label_ids = '{}' OR label_ids && zenith_current_labels()))"
        )
    )
    await conn.execute(text(f"GRANT SELECT ON {SCHEMA}.emb_flat, {SCHEMA}.chk_flat TO zenith_app"))
    await conn.execute(text(f"ANALYZE {SCHEMA}.emb_flat"))
    await conn.execute(text(f"ANALYZE {SCHEMA}.chk_flat"))


async def _assign(conn: AsyncConnection, space: tuple[str, str], populated: int) -> int:
    """One table saying which synthetic tenant each real chunk belongs to.

    Written once and reused by every rung, so the rows in a given tenant are the same rows at
    10 partitions and at 10,000 and the comparison across the ladder is of partition counts
    and of nothing else. Assignment is by `md5(chunk_id)` rather than by document, because
    tenants whose passages sit together in the corpus would make one partition's HNSW graph
    unrepresentatively coherent.
    """
    await conn.execute(
        text(
            f"CREATE TABLE {SCHEMA}.assign AS SELECT chunk_id, "
            "('00000000-0000-0000-0000-' || lpad((("
            "  ('x' || substr(md5(chunk_id::text), 1, 8))::bit(32)::bigint & 2147483647"
            f") % {populated})::text, 12, '0'))::uuid AS tenant_id "
            "FROM chunk_embeddings "
            "WHERE embedding_model = :model AND embedding_version = :version"
        ),
        {"model": space[0], "version": space[1]},
    )
    await conn.execute(text(f"ALTER TABLE {SCHEMA}.assign ADD PRIMARY KEY (chunk_id)"))
    return int((await conn.execute(text(f"SELECT count(*) FROM {SCHEMA}.assign"))).scalar_one())


async def _load(conn: AsyncConnection, space: tuple[str, str]) -> None:
    """Route the rows into whichever partitions hold them, and analyse only those.

    `ANALYZE` on the parent would touch every partition in one transaction and run out of the
    lock table at the top of the ladder — the same failure `_drop` avoids, in the other
    direction. Only the populated children have statistics worth gathering, and only the
    populated children are analysed.
    """
    await conn.execute(
        text(
            f"INSERT INTO {SCHEMA}.emb (chunk_id, tenant_id, embedding_model, "
            "embedding_version, embedding_half) "
            "SELECT a.chunk_id, a.tenant_id, e.embedding_model, e.embedding_version, "
            "       e.embedding_half "
            f"FROM {SCHEMA}.assign a JOIN chunk_embeddings e ON e.chunk_id = a.chunk_id "
            "WHERE e.embedding_model = :model AND e.embedding_version = :version"
        ),
        {"model": space[0], "version": space[1]},
    )
    await conn.execute(
        text(
            f"INSERT INTO {SCHEMA}.chk (id, tenant_id, label_ids) "
            f"SELECT chunk_id, tenant_id, '{{}}'::uuid[] FROM {SCHEMA}.assign"
        )
    )
    # `pg_relation_size > 0` rather than `reltuples`, which is -1 on a table that has never
    # been analysed — that is every partition here, so the obvious filter selects all ten
    # thousand of them and the tidy little ANALYZE loop becomes the lock exhaustion this
    # build is trying to avoid.
    await conn.execute(
        text(
            "DO $do$ DECLARE r record; BEGIN "
            "FOR r IN SELECT c.oid::regclass AS rel FROM pg_class c "
            "  JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"  WHERE n.nspname = '{SCHEMA}' AND c.relkind = 'r' "
            "  AND pg_relation_size(c.oid) > 0 LOOP "
            "  EXECUTE 'ANALYZE ' || r.rel; END LOOP; END $do$;"
        )
    )


async def _build(owner: AsyncEngine, shape: str, count: int, space: tuple[str, str]) -> float:
    """Everything for one rung, in autocommit so no transaction accumulates the lock table."""
    started = time.perf_counter()
    async with owner.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        by = "LIST (tenant_id)" if shape == "list" else "HASH (tenant_id)"
        ddl = _PARENT_DDL.format(schema=SCHEMA, by=by) + _POLICY_DDL.format(schema=SCHEMA)
        for statement in ddl.split(";"):
            if statement.strip():
                await conn.execute(text(statement))
        await _partitions(conn, shape, count)
        await _load(conn, space)
    return round(time.perf_counter() - started, 1)


async def _teardown(owner: AsyncEngine) -> None:
    """Drop the rung's partitioned pair but keep `assign` and the flat baseline."""
    async with owner.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        while True:
            names = [
                str(row[0])
                for row in await conn.execute(
                    text(
                        "SELECT c.relname FROM pg_class c JOIN pg_namespace n "
                        "ON n.oid = c.relnamespace WHERE n.nspname = :s AND c.relkind = 'r' "
                        "AND (c.relname LIKE 'e%' OR c.relname LIKE 'c%') "
                        "AND c.relname NOT IN ('emb_flat', 'chk_flat') LIMIT :n"
                    ),
                    {"s": SCHEMA, "n": BATCH},
                )
            ]
            if not names:
                break
            await conn.execute(
                text(f"DROP TABLE IF EXISTS {', '.join(f'{SCHEMA}.{n}' for n in names)} CASCADE")
            )
        await conn.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.emb, {SCHEMA}.chk CASCADE"))


# --- Question 2: who the partitioning is for -------------------------------------------


async def _distribution(conn: AsyncConnection) -> tuple[dict[str, object], int]:
    """The tenant size distribution this installation actually has.

    Two tenants and 13,549 passages cannot settle the question, and reporting it anyway is
    the point: the partitioning plan quotes 1.6M passages per tenant as though it were a
    measurement, and this is the only measurement there is. It is also, for what it is worth,
    the opposite of uniform.
    """
    rows = (
        await conn.execute(
            text(
                "SELECT t.id::text AS tenant, count(DISTINCT c.document_id) AS documents, "
                "count(c.id) AS passages FROM tenants t LEFT JOIN chunks c ON c.tenant_id = t.id "
                "GROUP BY t.id ORDER BY passages DESC"
            )
        )
    ).all()
    counted = [(str(row.tenant), int(row.documents), int(row.passages)) for row in rows]
    total = sum(passages for _, _, passages in counted)
    non_empty = [entry for entry in counted if entry[2] > 0]
    shares = [passages / total if total else 0.0 for _, _, passages in counted]
    tenants: list[dict[str, object]] = [
        {
            "tenant": tenant,
            "documents": documents,
            "passages": passages,
            "share": round(share, 4),
        }
        for (tenant, documents, passages), share in zip(counted, shares, strict=True)
    ]
    return {
        "tenants_provisioned": len(counted),
        "tenants_with_passages": len(non_empty),
        "passages": total,
        "largest_share": round(max(shares, default=0.0), 4),
        "uniform_share_would_be": round(1 / len(non_empty), 4) if non_empty else None,
        "note": (
            "Two tenants and 13,549 passages cannot settle a distribution. What it can do is "
            "refute the assumption the plan carries: the larger tenant holds a majority of "
            "the corpus, not the 1/T a uniform split would give it."
        ),
        "by_tenant": tenants,
    }, total


def _zipf(tenants: int, exponent: float) -> list[float]:
    weights = [1.0 / rank**exponent for rank in range(1, tenants + 1)]
    total = sum(weights)
    return [weight / total for weight in weights]


def _hash_sharing(tenants: int, modulus: int, largest: float) -> float:
    """What fraction of the corpus sits in the partition holding the largest tenant.

    Closed form, in expectation over a uniform hash. The largest tenant contributes its own
    share; the remaining `tenants/modulus - 1` tenants that land beside it contribute, on
    average, an equal slice of everything else. This is the whole cost of hash partitioning
    and it is arithmetic rather than something to measure: hashing does not split a tenant, it
    seats other tenants next to it.
    """
    if modulus >= tenants:
        return largest
    neighbours = tenants / modulus - 1
    return largest + (1 - largest) * neighbours / (tenants - 1)


def _tail(bytes_per_vector: float) -> dict[str, object]:
    """The benefit, parametrically in the largest tenant's share.

    **This cannot be settled here and the honest form of the answer is a function, not a
    number.** There is no customer size distribution in this repository, this installation has
    two tenants, and nothing in the corpus implies what a hundred customers would look like.
    So what follows is arithmetic over candidate shapes, and the shapes are labelled as
    assumptions.

    Zipf is the candidate because concentration in a multi-tenant archive is multiplicative —
    the customer with more documents this year is the one who had more last year — and a
    power law is the standard closed-form stand-in for that. Its exponent is not measured
    anywhere and 0.8 to 1.2 is a range, not a finding.

    Two benefits, and they point at different customers:

    - **Residency.** Today one HNSW graph holds every tenant and all of it must be resident
      to answer any query. Under per-tenant partitioning a query touches its own partition, so
      the resident floor for the *largest* tenant is `s_max` of the graph and the saving is
      `1 - s_max`. Uniformity makes that `1 - 1/T`, which is 99.5% at 200 tenants and is the
      figure the plan currently carries. Under Zipf it is far less, and the gap is the whole
      point of this section.
    - **Selectivity.** The RLS predicate on a shared graph admits `s_i` of it, and
      `eval/tenant-scale.json` measured what that costs: 43 of 50 candidates tenant-wide, 30
      of 50 under a label, nothing at all for 5 of 42 questions. Inside a partition the
      predicate admits everything. That benefit goes to the *small* tenant, whose filter was
      the tightest — and it is exactly inverted from residency, which goes to everyone except
      the large one.

    So partitioning is not one improvement with one beneficiary. It is a residency saving
    bounded by `1 - s_max` and a recall repair that is largest where `s_i` is smallest.
    """
    models: list[dict[str, object]] = []
    for tenants in MODELLED_TENANTS:
        uniform = 1 / tenants
        entry: dict[str, object] = {
            "tenants": tenants,
            "uniform": {
                "largest_share": round(uniform, 6),
                "residency_saving": round(1 - uniform, 4),
                "largest_tenant_passages_at_plan_corpus": round(PLAN_PASSAGES * uniform),
                "largest_tenant_index_gib": round(
                    PLAN_PASSAGES * uniform * bytes_per_vector / 1024**3, 1
                ),
            },
        }
        for exponent in ZIPF_EXPONENTS:
            shares = _zipf(tenants, exponent)
            largest = shares[0]
            # The tenant larger than 99% of tenants, by rank. Rank 1 is the largest, so the
            # ninety-ninth percentile is one hundredth of the way down the ordered list.
            p99 = shares[max(0, round(tenants * 0.01) - 1)]
            entry[f"zipf_{exponent}"] = {
                "largest_share": round(largest, 6),
                "p99_share": round(p99, 6),
                "residency_saving": round(1 - largest, 4),
                "times_the_uniform_assumption": round(largest / uniform, 1),
                "largest_tenant_passages_at_plan_corpus": round(PLAN_PASSAGES * largest),
                "largest_tenant_index_gib": round(
                    PLAN_PASSAGES * largest * bytes_per_vector / 1024**3, 1
                ),
                "hash_partition_share": {
                    str(modulus): round(_hash_sharing(tenants, modulus, largest), 6)
                    for modulus in HASH_MODULI
                },
            }
        models.append(entry)
    return {
        "form": (
            "residency_saving = 1 - s_max, where s_max is the largest tenant's share of the "
            "corpus. Under HASH with modulus P and T tenants the saving is "
            "1 - (s_max + (1 - s_max) * (T/P - 1)/(T - 1)), because hashing does not split a "
            "tenant, it seats other tenants beside it."
        ),
        "provenance": (
            "No customer size distribution exists in this repository and this installation "
            "has two tenants. The Zipf rows are assumptions with a stated exponent range and "
            "no measurement behind them. The plan should carry s_max as a parameter and ask "
            "the customer for their per-tenant document counts; substituting a real s_max "
            "into the form above is a one-line change and substituting a real distribution "
            "into a headline percentage is not."
        ),
        "bytes_per_vector": bytes_per_vector,
        "plan_corpus_passages": PLAN_PASSAGES,
        "models": models,
    }


# --- The sweep -------------------------------------------------------------------------


async def _postgres(conn: AsyncConnection) -> tuple[dict[str, object], int]:
    """The settings that bound this ladder, read from the server rather than assumed.

    `max_locks_per_transaction` times `max_connections` is not a per-transaction limit — it
    sizes one shared table that every backend draws from. So the ceiling this sweep finds is
    not "how many locks may one query take" but "how many such queries may be in flight at
    once", which is a concurrency limit dressed as a memory setting and is the reason a
    single-query timing hides it.
    """
    rows = (
        await conn.execute(
            text(
                "SELECT name, setting FROM pg_settings WHERE name IN ("
                "'max_locks_per_transaction', 'max_connections', 'max_prepared_transactions', "
                "'shared_buffers', 'work_mem', 'plan_cache_mode', 'enable_partition_pruning', "
                "'enable_partitionwise_join', 'server_version')"
            )
        )
    ).all()
    settings_map = {str(row.name): str(row.setting) for row in rows}
    slots = int(settings_map["max_locks_per_transaction"]) * (
        int(settings_map["max_connections"]) + int(settings_map["max_prepared_transactions"])
    )
    return {
        **settings_map,
        "lock_table_slots": slots,
        "lock_table_note": (
            "max_locks_per_transaction * max_connections sizes one table shared by the whole "
            "cluster; it is not a per-transaction allowance. A query taking N locks consumes "
            "N of these slots for the length of its transaction, so the partition ceiling is "
            "a concurrency limit, not a query limit."
        ),
    }, slots


async def _run(counts: tuple[int, ...]) -> int:
    owner = create_async_engine(settings.database_owner_url)
    app = create_async_engine(settings.database_url)
    profile = active_profile()
    started = time.perf_counter()
    report: dict[str, Any] = {}

    try:
        async with owner.connect() as conn:
            server, slots = await _postgres(conn)
            space = (
                await conn.execute(
                    text(
                        "SELECT embedding_model, embedding_version FROM chunk_embeddings "
                        "GROUP BY 1, 2 ORDER BY count(*) DESC LIMIT 1"
                    )
                )
            ).one()
            model, version = str(space.embedding_model), str(space.embedding_version)
            vector = str(
                (
                    await conn.execute(
                        text(
                            "SELECT embedding_half::text FROM chunk_embeddings "
                            "ORDER BY md5(chunk_id::text) LIMIT 1"
                        )
                    )
                ).scalar_one()
            )
            index_bytes = int(
                (
                    await conn.execute(
                        text(
                            "SELECT coalesce(pg_relation_size("
                            "to_regclass('ix_chunk_embeddings_hnsw_half')), 0)"
                        )
                    )
                ).scalar_one()
            )
            distribution, passages = await _distribution(conn)
            await conn.rollback()

        bytes_per_vector = round(index_bytes / passages, 1) if passages else 0.0
        print(
            f"{passages:,} passages, {distribution['tenants_with_passages']} tenants with rows, "
            f"index {index_bytes / 1e6:.0f} MB ({bytes_per_vector} B/vector)"
        )
        print(
            f"lock table: {server['max_locks_per_transaction']} x "
            f"{server['max_connections']} = {server['lock_table_slots']} slots\n"
        )

        await _drop(owner)
        async with owner.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
            await conn.execute(text(f"GRANT USAGE ON SCHEMA {SCHEMA} TO zenith_app"))
            rows = await _assign(conn, (model, version), POPULATED)
            await _flat(conn, (model, version))
        print(f"{rows:,} vectors assigned across {POPULATED} synthetic tenants")

        params: dict[str, object] = {
            "model": model,
            "version": version,
            "embedding": vector,
            "limit": WANTED,
        }
        queried = _tenant(0)

        baseline: dict[str, object] = {}
        for joined in (False, True):
            statement = (
                _dense_statement(SCHEMA, joined)
                .replace(".emb ", ".emb_flat ")
                .replace(".chk ", ".chk_flat ")
            )
            measured = await _arm(app, statement, params, queried, profile.hnsw_ef_search)
            baseline["join" if joined else "embeddings"] = measured
            print(
                f"  unpartitioned {'join      ' if joined else 'embeddings'}  "
                f"plan {measured.get('planning_ms')} ms  exec {measured.get('execution_ms')} ms  "
                f"locks {measured.get('locks_for_query')}"
            )
        print()

        ladder: list[dict[str, object]] = []
        for shape in SHAPES:
            for count in counts:
                print(f"  {shape} x {count}:", flush=True)
                try:
                    build_s = await _build(owner, shape, count, (model, version))
                except Exception as exc:  # noqa: BLE001 - a rung that cannot be built is data
                    detail = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
                    print(f"    build failed: {detail}", flush=True)
                    ladder.append({"shape": shape, "partitions": count, "build_error": detail})
                    await _teardown(owner)
                    continue
                print(f"    built in {build_s} s", flush=True)

                arms: dict[str, object] = {}
                for joined in (False, True):
                    key = "join" if joined else "embeddings"
                    statement = _dense_statement(SCHEMA, joined)
                    measured = await _arm(app, statement, params, queried, profile.hnsw_ef_search)
                    locks = measured.get("locks_for_query")
                    if isinstance(locks, int):
                        measured["pressure"] = _pressure(count, locks, slots, key)
                    arms[key] = measured
                    if "error" in measured:
                        print(f"    {key:<11} {measured['error']}", flush=True)
                    else:
                        print(
                            f"    {key:<11} plan {str(measured['planning_ms']):>9} ms  "
                            f"exec {str(measured['execution_ms']):>8} ms  "
                            f"removed {str(measured['subplans_removed']):>10}  "
                            f"locks {str(measured['locks_for_query']):>6}  "
                            f"median {str(measured['median_ms']):>8} ms",
                            flush=True,
                        )
                prepared = await _prepared(
                    app, _dense_statement(SCHEMA, False), params, queried, profile.hnsw_ef_search
                )
                ladder.append(
                    {
                        "shape": shape,
                        "partitions": count,
                        "build_s": build_s,
                        "arms": arms,
                        "prepared": prepared,
                    }
                )
                await _teardown(owner)

        report = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "took_s": round(time.perf_counter() - started, 1),
            "profile": profile.name,
            "postgres": server,
            "corpus": {
                "passages": passages,
                "production_index_bytes": index_bytes,
                "bytes_per_vector": bytes_per_vector,
                "embedding_space": {"model": model, "version": version},
            },
            "build": {
                "populated_tenants": POPULATED,
                "rows": rows,
                "rows_per_tenant": round(rows / POPULATED),
                "indexes_per_partition": {
                    "emb": ["hnsw(embedding_half)"],
                    "chk": ["btree(id)"],
                    "note": (
                        "The installation's `chunks` carries five indexes and a primary key, "
                        "and `chunk_embeddings` a primary key besides the HNSW index. Locks "
                        "are taken per relation, so multiply the measured lock count by the "
                        "real index count to project: this ladder's 2 relations per "
                        "embeddings partition become 3, and its 2 per chunks partition "
                        "become 6."
                    ),
                },
            },
            "unpartitioned": baseline,
            "ceiling": _ceiling(ladder, slots),
            "ladder": ladder,
            "distribution": distribution,
            "benefit": _tail(bytes_per_vector),
            "caveats": [
                "Latency on this machine is contended and memory-starved: 7.75 GB for the "
                "whole VM and 128 MB of shared_buffers, shared with the production corpus "
                "and with whatever else is running. Execution and median figures are "
                "indicative. Planning time, Subplans Removed and the lock counts are the "
                "sound half — they are properties of the plan and of the catalogue, not of "
                "how busy the machine was.",
                "Every partition beyond the eight populated ones is empty. That is "
                "deliberate and it does not affect what is being measured: planning time and "
                "lock count depend on the number of partitions, not on their contents, and "
                "holding the queried partition's row count constant is what makes execution "
                "times comparable between rungs.",
                "The scratch tables carry one index per partition. The production tables "
                "carry more, so every lock count here is a floor - see build.indexes_per_"
                "partition for the multiplier.",
                "enable_partitionwise_join is off, which is the installation's setting. With "
                "it on, the join arm would plan per-partition joins and both the planning "
                "cost and the pruning behaviour of that arm would change. That is a separate "
                "measurement and this sweep does not make a claim about it.",
                "The query vector is a stored passage embedding rather than a question "
                "embedded through TEI. Question-to-passage asymmetry changes which rows come "
                "back; it does not change planning time, lock count or Subplans Removed, "
                "which is everything this sweep concludes from.",
                "The lock table grew past its nominal size and queries above it still ran - "
                "see ceiling.observed_note. That surplus is unreserved shared memory shared "
                "with every other backend, so a rung that passed here on an idle "
                "installation is not a rung that passes under load.",
                "The PREPARE/EXECUTE arm runs the embeddings shape only. The join shape "
                "exhausts the lock table two rungs earlier and a prepared-statement figure "
                "for it would exist at 10 and 100 partitions and nowhere the answer matters.",
                "Execution time falls when the table is partitioned, and that is pruning "
                "working rather than a machine artefact - the scan goes from 13,549 rows "
                "under a policy filter to the queried tenant's 1,705. It is also the wrong "
                "way round for the question being asked: what grows with partition count is "
                "planning, and past about a thousand partitions planning is the whole query.",
                "The tenant size distribution cannot be settled by this installation. The "
                "Zipf rows under benefit.models are assumptions, labelled as such, and the "
                "number the plan should carry is the parametric form beside them.",
            ],
        }
        REPORT.write_text(json.dumps(report, indent=2) + "\n")
        print(f"\nWritten to {REPORT.name}")
    finally:
        # Unconditional, and verified rather than assumed. The rule was written after two
        # undeclared `SECURITY DEFINER` functions from an earlier investigation were found
        # still installed months later.
        await _drop(owner)
        async with owner.connect() as conn:
            left = int(
                (
                    await conn.execute(
                        text("SELECT count(*) FROM pg_namespace WHERE nspname = :s"), {"s": SCHEMA}
                    )
                ).scalar_one()
            )
            definers = int(
                (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n "
                            "ON n.oid = p.pronamespace WHERE p.prosecdef AND n.nspname = 'public'"
                        )
                    )
                ).scalar_one()
            )
            await conn.rollback()
        print(f"scratch schemas left: {left}; SECURITY DEFINER in public: {definers} (expected 7)")
        if report:
            report["verification"] = {
                "scratch_schemas_left": left,
                "security_definers_in_public": definers,
                "expected_security_definers": 7,
                "clean": left == 0 and definers == 7,
            }
            REPORT.write_text(json.dumps(report, indent=2) + "\n")
        await app.dispose()
        await owner.dispose()

    return 0


#: How `python -m eval` finds this sweep. Declared here rather than listed in `__main__.py`,
#: so adding a measurement is adding a file and nothing else.
COMMAND = "partition-shape"
USAGE = "partition-shape [--counts N,N,...]"


def run(counts: tuple[int, ...] = COUNTS) -> int:
    return asyncio.run(_run(counts))


def cli(argv: list[str]) -> int:
    """`--counts N,N,...`, or the default ladder."""
    if "--counts" in argv:
        return run(tuple(int(n) for n in argv[argv.index("--counts") + 1].split(",")))
    return run()
