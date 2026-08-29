# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""What the dense half costs at each modulus, and whether that cost is a choice.

`modulus-cost.json` costed the modulus and found the lexical half flat — 0.223 ms of planning
at 32 partitions and 0.273 ms at 512, 12 locks at every rung — while a whole request grew from
17.16 ms at 128 to 37.39 ms at 256. `unpruned-queries.json` explains the flat half: migration
0026 lifted `zenith_current_tenant()` into a plpgsql local and used that local as a SQL
qualifier, and a local is a parameter the planner may fold into a constant. A `STABLE` function
is not a constant, so it can only prune at executor startup — after every partition has been
opened and priced.

The dense join still prunes at executor startup, and after that fix it is essentially the whole
remaining modulus cost. Stage 02 called it irreducible: the dense predicate *is* the RLS
policy, and handing the planner a constant there would break invariant 1.

**This file tests that, and the reason it can even be asked is `SECURITY INVOKER`.** The
lexical function is `SECURITY DEFINER` because ParadeDB's custom scan will not run under a
policy-qualified scan (ADR 0002's F18), so its tenant clause runs with no policy behind it and
a tenant argument there really would be a leak. The dense query needs no such thing. Under
`SECURITY INVOKER` the body runs with the caller's policies still on, and the added qualifier
is redundant *with* the policy rather than a substitute *for* it.

`eval/dense-plan-time-pruning.sql` is the isolation half of the answer, at one modulus and in
full detail. This file is the ladder: what the shape costs at 32, 64, 128 and 256, whether the
saving is real and whether the rows are the same ones. The isolation checks are repeated at
every rung here too, because a property that holds at 32 partitions and not at 256 is exactly
the kind of thing a single-modulus probe cannot see.

## The bars, set before the run

Written here first so the result cannot be read backwards afterwards. A failed bar stays in
this file rather than being rewritten.

**Bar 1 — the arms have to be different plans.** At every partitioned rung the `today` arm
must carry two `Append`s, one per partitioned relation, each reporting
`Subplans Removed = modulus - 1`; the `local` arm must carry none at all and must name exactly
`PARTITIONS_WHEN_PRUNED` relations. Same numbers on both sides is a harness measuring itself,
which is what three null results did this week.

**The signal is `Subplans Removed` and not the count of partitions named**, and getting that
the wrong way round is how this bar was first written. `modulus_cost.py` can read the count,
because the arm it watches prunes *nothing* — the lexical tenant clause sits inside the
Tantivy operand where it is not a partition-key qualifier at all, so every partition appears in
the plan. The dense arm does prune; it prunes late. `EXPLAIN ANALYZE` prints only the subplans
that survived executor startup, so **both** arms here name two partitions and only the
`Subplans Removed` lines say which of them opened and priced two hundred and fifty-six paths
to get there.

**Bar 2 — the rows have to be identical, to the bit.** The same chunk ids at the same ranks,
and `max_score_delta` exactly `0.0`. The lexical fix was allowed a uniform score shift because
ParadeDB folds a SQL qualifier into the BM25 sum as one more `must` clause; cosine distance
has no such excuse. Anything but zero here and the arm is not a candidate, however fast.

**Bar 3 — the qualifier must never be the thing that decides.** At every rung, for every pair
of (session context, requested tenant) over all `TENANTS`, the diagonal returns rows and every
other cell returns none. Counted against `assign`, which carries no policy, so a leak cannot
hide inside the arm being tested.

**Bar 4 — Bar 3 has to survive the plan cache.** Postgres considers a generic plan after five
custom ones and a generic plan has no parameter values to fold. Nine calls under one tenant,
then another tenant on the same backend, then the same again with `plan_cache_mode =
force_generic_plan`. A fix that isolates correctly until the sixth execution is a leak with a
delay.

**Bar 5 — the locks must stop growing.** `lock-budget.json` sized `max_locks_per_transaction`
around a dense arm that locks every partition. The whole point of pruning at plan time is that
the pruned partitions are never locked, so `f_local` must stay under `LOCK_CEILING` at every
rung while `f_shipped` grows with the modulus. A saving in planning time with the lock count
unmoved is the failure this week's first lexical fix produced — `Subplans Removed: 255` and
not one lock fewer.

## Which numbers transfer and which do not

The same caveat `partition-shape.json` and `modulus-cost.json` carry. **Planning time,
`Subplans Removed`, the partitions a plan names and lock counts are properties of the plan and
the catalogue, and they transfer.** Milliseconds of execution on 13,549 passages split eight
ways do not: 1,694 rows a tenant is not a corpus, and no execution figure here says anything
about a real one. Execution time is recorded because a fix that halved planning and doubled
execution would be no fix, and for no other purpose.

The lock table is cluster-wide and shared with whatever else is running on this machine. A
lock reading is therefore a difference taken inside one transaction, and a rung that fails at a
size that worked a moment ago is re-run before it is believed.

## Read-only with respect to the corpus

Everything is built inside `zenith_denseplan`, dropped in a `finally`, and verified gone.
`chunks` and `chunk_embeddings` are read and never written. The teardown drops in batches with
a per-table fallback, for the reason `modulus_cost.py` records: `DROP SCHEMA ... CASCADE` over
hundreds of partitions is one transaction taking an `AccessExclusiveLock` on every object in
it, and it fails under exactly the pressure this file exists to measure.

    docker exec zenith-api-1 python -m eval dense-plan-time [--partitions 32,64,128,256]
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.core.config import settings
from app.core.partitions import LABEL_POLICY, TENANT_POLICY
from app.features.retrieval.search import CANDIDATES, ITERATIVE_SCAN

REPORT = Path(__file__).parent / "dense-plan-time.json"

#: One schema, one name, dropped in a `finally`, so a leftover says who left it. Distinct from
#: `dense-plan-time-pruning.sql`'s `zenith_denseplan_iso` so the two can run at once.
SCHEMA = "zenith_denseplan"

#: `None` is the unpartitioned control. It is the only rung that says how much of the cost is
#: partitioning at all rather than the modulus.
RUNGS: tuple[int | None, ...] = (None, 32, 64, 128, 256)

#: Synthetic tenants. Eight rather than two because Bar 3 is a grid and a grid of two proves
#: very little; and few enough that the grid stays 64 cells at every rung.
TENANTS = 8

#: One partition per relation, and the dense join has two of them. What Bar 1 requires of the
#: pruned arm: `emb` and `chk`, once each, and no `Append` above either.
PARTITIONS_WHEN_PRUNED = 2

#: Locks the pruned arm may hold. Two partitions, their indexes, the two parents and the
#: catalogue entries a plpgsql call touches — measured at 14 at modulus 32 in
#: `dense-plan-time-pruning.sql`. The ceiling is set with room above that and below anything
#: that could be called growth.
LOCK_CEILING = 32

#: Eleven plans per arm, as `modulus_cost.py` takes. Enough to cross the five-execution
#: threshold at which Postgres will consider a generic plan, which is a thing that has to be
#: seen rather than assumed away.
PLAN_REPEATS = 11

#: Timed calls of each function, for the wall clock the product actually pays. `EXPLAIN` on a
#: call reports `Function Scan` and one number, so the planning inside a plpgsql body is not
#: visible to it; the clock is.
CALL_REPEATS = 9

#: Partitions created per `DO` block. `modulus_cost.py`'s number, for its reason: one
#: transaction per partition is slow and one transaction for all of them runs out of locks.
BATCH = 16

#: What the dense half asks for. Imported rather than repeated.
WANTED = CANDIDATES

#: Two labels and an unlabelled third. Bar 5 of the SQL file: `chunk_embeddings` is
#: tenant-scoped only and label isolation arrives through the join to `chunks`, so a corpus
#: where every row is unlabelled cannot tell a working join from a dropped one.
LABEL_A = "aaaaaaaa-0000-0000-0000-000000000001"
LABEL_B = "bbbbbbbb-0000-0000-0000-000000000002"


def _tenant(index: int) -> str:
    return f"00000000-0000-0000-0000-{index:012d}"


def _literal(value: str) -> str:
    """A string as a SQL literal, for the one place a bind parameter is not allowed.

    `EXECUTE` takes expressions rather than protocol parameters, so the prepared-plan arms
    have to write their arguments in. Every value that reaches here is written in this file or
    read out of the scratch schema this file built, and the doubling is here so that stays
    true if someone later passes something that is not.
    """
    return "'" + value.replace("'", "''") + "'"


# --- Reading a plan ----------------------------------------------------------------------

_PLANNING = re.compile(r"Planning Time: ([\d.]+) ms")
_EXECUTION = re.compile(r"Execution Time: ([\d.]+) ms")
_REMOVED = re.compile(r"Subplans Removed: (\d+)")
#: A scan node naming one of this schema's partitions — `e17`, `c204` — and never a parent,
#: whose relations are `emb` and `chk`.
_PARTITION = re.compile(r"\bon ([ec]\d+)\b")
#: The line that is the whole security argument. The planner reduces the RLS policy and the
#: folded qualifier to one comparison between the session GUC and the parameter; if they
#: disagree it is false and nothing below it runs.
_ONE_TIME = re.compile(r"One-Time Filter:")


def _read_plan(lines: list[str]) -> dict[str, Any]:
    """What a partitioned plan has to state about itself.

    `Subplans Removed` appears only under `ANALYZE`: pruning on a `STABLE` key happens at
    executor startup, so a plan that was never started cannot report it. Collected as a list,
    one entry per `Append`, because the dense join has two partitioned relations and a single
    number would report one relation's pruning as the whole query's.

    A plan that pruned at *plan* time and one that pruned nothing both print an empty
    `Subplans Removed` list — the first has no `Append` left to report it and the second has
    nothing to report. Counting the partitions the plan names separates them, which is why
    Bar 1 is written in those terms.

    The scan lines are truncated. A `halfvec(1024)` literal in an `Order By` is thirteen
    kilobytes of the same plan repeated once per rung per arm, and the report is meant to be
    read.
    """
    joined = "\n".join(lines)
    planning = _PLANNING.search(joined)
    execution = _EXECUTION.search(joined)
    removed = [int(match) for match in _REMOVED.findall(joined)]
    scans = [
        line.strip().removeprefix("->  ").removeprefix("Parallel ")[:120]
        for line in lines
        if "Scan" in line
    ]
    return {
        "planning_ms": float(planning.group(1)) if planning else None,
        "execution_ms": float(execution.group(1)) if execution else None,
        "subplans_removed": removed,
        "appends": len(removed),
        "partitions_in_plan": len(_PARTITION.findall(joined)),
        "policy_became_a_one_time_filter": bool(_ONE_TIME.search(joined)),
        "leaf_scans": scans[:4],
    }


def _spread(values: list[float]) -> dict[str, float]:
    """Median, tail and floor of a set of readings, in the order they were taken.

    `median_warm` drops the first reading. The first plan on a connection is measurably
    dearer — the catalogue rows for a few hundred partitions have to be read before anything
    can be planned — and that is a real cost a pooled connection pays once, not once per
    query. Both are reported because neither alone is the answer.
    """
    if not values:
        return {}
    ordered = sorted(values)
    warm = sorted(values[1:]) or ordered
    return {
        "median": round(statistics.median(ordered), 3),
        "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
        "max": round(ordered[-1], 3),
        "min": round(ordered[0], 3),
        "median_warm": round(statistics.median(warm), 3),
        "first": round(values[0], 3),
    }


async def _explain(conn: AsyncConnection, statement: str) -> list[str]:
    rows = await conn.execute(
        text(f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY ON) {statement}")
    )
    return [str(row[0]) for row in rows]


async def _locks(conn: AsyncConnection) -> int:
    """Locks held by this backend right now, counted inside the transaction that took them.

    Every one is released at commit, so this is the only place the number exists. A difference
    against a reading taken before the statement removes the virtual transaction id and the
    other per-transaction constants.
    """
    return int(
        (
            await conn.execute(text("SELECT count(*) FROM pg_locks WHERE pid = pg_backend_pid()"))
        ).scalar_one()
    )


async def _context(conn: AsyncConnection, tenant: str, labels: str = "") -> None:
    """The session the application would have, set the way `set_rls_context` sets it.

    Identical but for the timeout, which is raised above the product's 10 s so that a rung
    which took too long to plan is reported as the planning cost it is rather than as a
    failure to plan. Serial, because a parallel plan over hundreds of partitions asks for a
    shared segment larger than this container's `/dev/shm` and dies naming the wrong resource
    entirely — `eval/partition_shape.py` and `eval/modulus_cost.py` do the same.
    """
    await conn.execute(
        text(
            "SELECT set_config('zenith.tenant_id', :tenant, true), "
            "       set_config('zenith.label_ids', :labels, true), "
            "       set_config('zenith.user_id', '', true), "
            "       set_config('zenith.reads_all_history', 'false', true)"
        ),
        {"tenant": tenant, "labels": labels},
    )
    await conn.execute(text("SET LOCAL statement_timeout = '300s'"))
    await conn.execute(text(f"SET LOCAL hnsw.iterative_scan = {ITERATIVE_SCAN}"))
    await conn.execute(text("SET LOCAL max_parallel_workers_per_gather = 0"))


# --- The schema --------------------------------------------------------------------------

_COLUMNS_EMB = """
  chunk_id          uuid NOT NULL,
  tenant_id         uuid NOT NULL,
  embedding_model   varchar NOT NULL,
  embedding_version varchar NOT NULL,
  embedding_half    halfvec(1024) NOT NULL
"""

_COLUMNS_CHK = """
  id           uuid NOT NULL,
  document_id  uuid NOT NULL,
  tenant_id    uuid NOT NULL,
  label_ids    uuid[] NOT NULL DEFAULT '{}',
  text         varchar NOT NULL
"""

#: The installation's index set for the two relations the dense join reads, replayed on the
#: parents so Postgres propagates one copy to every partition. The bm25 index is not here:
#: this file measures the dense half and a Tantivy index on every partition would triple the
#: build for relations no arm below reads.
_INDEX_DDL = (
    "ALTER TABLE {schema}.emb ADD PRIMARY KEY (chunk_id, embedding_model, "
    "embedding_version, tenant_id)",
    "CREATE INDEX ON {schema}.emb USING hnsw (embedding_half halfvec_cosine_ops) "
    "WITH (m = 16, ef_construction = 64)",
    "ALTER TABLE {schema}.chk ADD PRIMARY KEY (id, tenant_id)",
    "CREATE INDEX ON {schema}.chk USING gin (label_ids)",
    "CREATE INDEX ON {schema}.chk (tenant_id)",
)

_POLICY_DDL = (
    "ALTER TABLE {schema}.emb ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE {schema}.chk ENABLE ROW LEVEL SECURITY",
    "CREATE POLICY emb_isolation ON {schema}.emb USING (tenant_id = zenith_current_tenant())",
    "CREATE POLICY chk_isolation ON {schema}.chk "
    "USING (tenant_id = zenith_current_tenant() "
    "AND (label_ids = '{{}}' OR label_ids && zenith_current_labels()))",
    "GRANT SELECT ON {schema}.emb, {schema}.chk, {schema}.assign TO zenith_app",
)

#: The four function arms, all `SECURITY INVOKER`, all running under the caller's policies.
#:
#: `f_shipped` is `search.dense()` with the schema changed and nothing else. `f_local` adds
#: two things and no others: the tenant is read from `zenith_current_tenant()` into a plpgsql
#: local, and that local appears as an ordinary SQL qualifier. The join is untouched — it is
#: where label isolation lives, `chunk_embeddings` being tenant-scoped only.
#:
#: `f_guc` is the control that says which half of `f_local` does the work: the same redundant
#: qualifier, written as the `STABLE` call instead of a local.
#:
#: `f_dynamic` is the third form. It writes the tenant into the statement text and re-parses
#: per call, so it holds a real constant rather than a parameter and cannot be undone by the
#: plan cache choosing a generic plan. It is the answer to the one limit
#: `unpruned-plpgsql-pruning.sql` admitted it could not close, and it is measured here to find
#: out what the re-parse costs.
#:
#: `f_forced` is not a candidate for anything. It takes the tenant as an argument — the shape
#: the lexical file refused, because there the function is `SECURITY DEFINER` and the argument
#: would be the only thing between one customer and another's passages. Here it exists so the
#: grid can make the qualifier and the policy disagree on purpose.
_FUNCTION_DDL = """
CREATE FUNCTION {schema}.f_shipped(q halfvec(1024), mdl text, ver text, want int)
RETURNS TABLE(chunk_id uuid, score double precision)
LANGUAGE plpgsql STABLE SECURITY INVOKER
AS $body$
BEGIN
  RETURN QUERY
  SELECT c.id, 1 - (e.embedding_half <=> q)
  FROM {schema}.emb e
  JOIN {schema}.chk c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id
  WHERE e.embedding_model = mdl AND e.embedding_version = ver
  ORDER BY e.embedding_half <=> q
  LIMIT want;
END;
$body$;
CREATE FUNCTION {schema}.f_local(q halfvec(1024), mdl text, ver text, want int)
RETURNS TABLE(chunk_id uuid, score double precision)
LANGUAGE plpgsql STABLE SECURITY INVOKER
AS $body$
DECLARE v_tenant uuid := zenith_current_tenant();
BEGIN
  IF v_tenant IS NULL THEN RETURN; END IF;
  RETURN QUERY
  SELECT c.id, 1 - (e.embedding_half <=> q)
  FROM {schema}.emb e
  JOIN {schema}.chk c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id
  WHERE e.tenant_id = v_tenant
    AND e.embedding_model = mdl AND e.embedding_version = ver
  ORDER BY e.embedding_half <=> q
  LIMIT want;
END;
$body$;
CREATE FUNCTION {schema}.f_guc(q halfvec(1024), mdl text, ver text, want int)
RETURNS TABLE(chunk_id uuid, score double precision)
LANGUAGE plpgsql STABLE SECURITY INVOKER
AS $body$
BEGIN
  RETURN QUERY
  SELECT c.id, 1 - (e.embedding_half <=> q)
  FROM {schema}.emb e
  JOIN {schema}.chk c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id
  WHERE e.tenant_id = zenith_current_tenant()
    AND e.embedding_model = mdl AND e.embedding_version = ver
  ORDER BY e.embedding_half <=> q
  LIMIT want;
END;
$body$;
CREATE FUNCTION {schema}.f_dynamic(q halfvec(1024), mdl text, ver text, want int)
RETURNS TABLE(chunk_id uuid, score double precision)
LANGUAGE plpgsql STABLE SECURITY INVOKER
AS $body$
DECLARE v_tenant uuid := zenith_current_tenant();
BEGIN
  IF v_tenant IS NULL THEN RETURN; END IF;
  RETURN QUERY EXECUTE format(
    'SELECT c.id, 1 - (e.embedding_half <=> $1) '
    'FROM {schema}.emb e '
    'JOIN {schema}.chk c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id '
    'WHERE e.tenant_id = %L AND e.embedding_model = $2 AND e.embedding_version = $3 '
    'ORDER BY e.embedding_half <=> $1 LIMIT $4', v_tenant)
  USING q, mdl, ver, want;
END;
$body$;
CREATE FUNCTION {schema}.f_forced(v_tenant uuid, q halfvec(1024), mdl text, ver text, want int)
RETURNS TABLE(chunk_id uuid, score double precision)
LANGUAGE plpgsql STABLE SECURITY INVOKER
AS $body$
BEGIN
  RETURN QUERY
  SELECT c.id, 1 - (e.embedding_half <=> q)
  FROM {schema}.emb e
  JOIN {schema}.chk c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id
  WHERE e.tenant_id = v_tenant
    AND e.embedding_model = mdl AND e.embedding_version = ver
  ORDER BY e.embedding_half <=> q
  LIMIT want;
END;
$body$;
"""

_GRANT_DDL = """
GRANT EXECUTE ON FUNCTION
  {schema}.f_shipped(halfvec(1024), text, text, int),
  {schema}.f_local(halfvec(1024), text, text, int),
  {schema}.f_guc(halfvec(1024), text, text, int),
  {schema}.f_dynamic(halfvec(1024), text, text, int),
  {schema}.f_forced(uuid, halfvec(1024), text, text, int)
TO zenith_app
"""

#: `EXPLAIN` on a call does not descend into a plpgsql body — it reports `Function Scan` and
#: one number — so the plans are exhibited through `PREPARE`/`EXECUTE`. That is the same
#: machinery rather than an approximation: a plpgsql statement *is* an SPI prepared plan whose
#: locals are its parameters, and it obeys `plan_cache_mode` exactly as these do. The
#: functions above are still what the equivalence, lock and isolation sections call, so
#: correctness is measured on the real thing.
_PREPARES = {
    "today": """
PREPARE p_today(halfvec(1024), text, text, int) AS
SELECT c.id, 1 - (e.embedding_half <=> $1) AS score
FROM {schema}.emb e
JOIN {schema}.chk c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id
WHERE e.embedding_model = $2 AND e.embedding_version = $3
ORDER BY e.embedding_half <=> $1 LIMIT $4
""",
    "local": """
PREPARE p_local(uuid, halfvec(1024), text, text, int) AS
SELECT c.id, 1 - (e.embedding_half <=> $2) AS score
FROM {schema}.emb e
JOIN {schema}.chk c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id
WHERE e.tenant_id = $1 AND e.embedding_model = $3 AND e.embedding_version = $4
ORDER BY e.embedding_half <=> $2 LIMIT $5
""",
    "guc": """
PREPARE p_guc(halfvec(1024), text, text, int) AS
SELECT c.id, 1 - (e.embedding_half <=> $1) AS score
FROM {schema}.emb e
JOIN {schema}.chk c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id
WHERE e.tenant_id = zenith_current_tenant()
  AND e.embedding_model = $2 AND e.embedding_version = $3
ORDER BY e.embedding_half <=> $1 LIMIT $4
""",
}


# --- Building and tearing down -----------------------------------------------------------


async def _drop_tables(conn: AsyncConnection, keep: tuple[str, ...] = ()) -> int:
    """Drop the schema's tables in batches, with a one-at-a-time fallback.

    `DROP SCHEMA ... CASCADE` over hundreds of partitions is one transaction taking an
    `AccessExclusiveLock` on every object in it, which is more locks than the shared table
    holds under the pressure this file measures. The tidy-up then fails and the schema
    survives, which is the one outcome this file may not produce.

    **A partitioned parent is `relkind = 'p'`, not `'r'`.** `modulus_cost.py` lost a run to
    that: every rung after the first failed with `relation "emb" already exists` because the
    parents survived a teardown that only looked for ordinary tables.
    """
    rows = list(
        await conn.execute(
            text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :s AND c.relkind IN ('r', 'p') AND NOT (c.relname = ANY(:k)) "
                "ORDER BY c.relkind DESC, c.relname"
            ),
            {"s": SCHEMA, "k": list(keep)},
        )
    )
    names = [str(row.relname) for row in rows]
    dropped = 0
    for low in range(0, len(names), BATCH):
        batch = names[low : low + BATCH]
        try:
            await conn.execute(
                text(f"DROP TABLE IF EXISTS {', '.join(f'{SCHEMA}.{n}' for n in batch)} CASCADE")
            )
            dropped += len(batch)
        except Exception:  # noqa: BLE001 - under real pressure even sixteen is too many
            for name in batch:
                with contextlib.suppress(Exception):
                    await conn.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.{name} CASCADE"))
                    dropped += 1
    return dropped


async def _drop(owner: AsyncEngine) -> None:
    async with owner.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await _drop_tables(conn)
        await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))


async def _teardown(owner: AsyncEngine) -> None:
    """Drop the rung's pair, keeping `assign` so the next rung places the same rows."""
    async with owner.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        for signature in (
            "f_shipped(halfvec(1024), text, text, int)",
            "f_local(halfvec(1024), text, text, int)",
            "f_guc(halfvec(1024), text, text, int)",
            "f_dynamic(halfvec(1024), text, text, int)",
            "f_forced(uuid, halfvec(1024), text, text, int)",
        ):
            with contextlib.suppress(Exception):
                await conn.execute(text(f"DROP FUNCTION IF EXISTS {SCHEMA}.{signature} CASCADE"))
        await _drop_tables(conn, keep=("assign",))


async def _assign(owner: AsyncEngine, space: tuple[str, str]) -> dict[str, object]:
    """Which synthetic tenant and which labels each real chunk carries.

    Written once and reused by every rung, so the rows in a given tenant are the same rows at
    64 partitions and at 256 and the comparison is of moduli and nothing else. It carries no
    policy on purpose: it is the ground truth Bar 3 counts against, and an arm that could
    filter the table it is being checked against would be checking itself.

    Uniform over the eight tenants rather than Zipf. `modulus-cost.json` draws Zipf because it
    is measuring *widening*, which is a question about how much of a partition a tenant
    shares. Nothing here is a question about size: Bar 1 and Bar 5 are properties of the plan,
    and Bars 2 to 4 are about which rows come back rather than how many.
    """
    async with owner.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        # Without this the application role cannot see the schema at all and every arm returns
        # `permission denied`, which the first run of `modulus_cost.py` recorded as an error
        # per rung rather than as a measurement.
        await conn.execute(text(f"GRANT USAGE ON SCHEMA {SCHEMA} TO zenith_app"))
        await conn.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.assign"))
        await conn.execute(
            text(
                f"CREATE TABLE {SCHEMA}.assign AS "
                "SELECT chunk_id, "
                "  ('00000000-0000-0000-0000-' || lpad(((("
                "     'x' || substr(md5(chunk_id::text), 1, 8))::bit(32)::bigint "
                "     & 2147483647) % :t + 1)::text, 12, '0'))::uuid AS tenant_id, "
                "  CASE ((('x' || substr(md5(chunk_id::text), 9, 8))::bit(32)::bigint "
                "         & 2147483647) % 3) "
                "    WHEN 0 THEN ARRAY[CAST(:a AS uuid)] "
                "    WHEN 1 THEN ARRAY[CAST(:b AS uuid)] "
                "    ELSE '{}'::uuid[] END AS label_ids "
                "FROM chunk_embeddings "
                "WHERE embedding_model = :model AND embedding_version = :version"
            ),
            {"t": TENANTS, "a": LABEL_A, "b": LABEL_B, "model": space[0], "version": space[1]},
        )
        await conn.execute(text(f"ALTER TABLE {SCHEMA}.assign ADD PRIMARY KEY (chunk_id)"))
        await conn.execute(text(f"GRANT SELECT ON {SCHEMA}.assign TO zenith_app"))
        rows = list(
            await conn.execute(
                text(
                    f"SELECT tenant_id::text AS tid, count(*) AS n FROM {SCHEMA}.assign "
                    "GROUP BY 1 ORDER BY 1"
                )
            )
        )
        labelled = list(
            await conn.execute(
                text(
                    f"SELECT coalesce(label_ids[1]::text, 'none') AS lab, count(*) AS n "
                    f"FROM {SCHEMA}.assign GROUP BY 1 ORDER BY 1"
                )
            )
        )
    return {
        "tenants": len(rows),
        "rows": sum(int(row.n) for row in rows),
        "rows_per_tenant": {str(row.tid): int(row.n) for row in rows},
        "rows_per_label": {str(row.lab): int(row.n) for row in labelled},
    }


async def _build(owner: AsyncEngine, modulus: int | None, space: tuple[str, str]) -> float:
    """Everything for one rung, in autocommit so no transaction accumulates the lock table.

    Every partition gets `ENABLE ROW LEVEL SECURITY` and a policy of its own in the same batch
    as its `CREATE TABLE`. **A partition inherits neither** — CLAUDE.md's first invariant, and
    `eval/partition-rls.sql` demonstrates it against this database. It is not decoration for a
    planning measurement either: a policy on a partition is an expression the planner fetches
    and applies for every unpruned child, so a ladder built without them would understate
    planning at exactly the rung where planning is what is being watched.
    """
    started = time.perf_counter()
    by = " PARTITION BY HASH (tenant_id)" if modulus else ""
    async with owner.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(f"CREATE TABLE {SCHEMA}.emb ({_COLUMNS_EMB}){by}"))
        await conn.execute(text(f"CREATE TABLE {SCHEMA}.chk ({_COLUMNS_CHK}){by}"))
        for template in _INDEX_DDL + _POLICY_DDL:
            await conn.execute(text(template.format(schema=SCHEMA)))
        for body in _FUNCTION_DDL.format(schema=SCHEMA).split("$body$;"):
            if body.strip():
                await conn.execute(text(body + "$body$;"))
        await conn.execute(text(_GRANT_DDL.format(schema=SCHEMA)))
        if modulus:
            emb_policy = TENANT_POLICY.replace("'", "''")
            chk_policy = LABEL_POLICY.replace("'", "''")
            for low in range(0, modulus, BATCH):
                high = min(low + BATCH, modulus)
                await conn.execute(
                    text(
                        "DO $do$ DECLARE i int; BEGIN "
                        f"FOR i IN {low}..{high - 1} LOOP "
                        f"  EXECUTE 'CREATE TABLE {SCHEMA}.e' || i "
                        f"       || ' PARTITION OF {SCHEMA}.emb "
                        f"FOR VALUES WITH (MODULUS {modulus}, REMAINDER ' || i || ')'; "
                        f"  EXECUTE 'ALTER TABLE {SCHEMA}.e' || i "
                        "       || ' ENABLE ROW LEVEL SECURITY'; "
                        f"  EXECUTE 'CREATE POLICY e' || i || '_isolation ON {SCHEMA}.e' || i "
                        f"       || ' USING ({emb_policy})'; "
                        f"  EXECUTE 'CREATE TABLE {SCHEMA}.c' || i "
                        f"       || ' PARTITION OF {SCHEMA}.chk "
                        f"FOR VALUES WITH (MODULUS {modulus}, REMAINDER ' || i || ')'; "
                        f"  EXECUTE 'ALTER TABLE {SCHEMA}.c' || i "
                        "       || ' ENABLE ROW LEVEL SECURITY'; "
                        f"  EXECUTE 'CREATE POLICY c' || i || '_isolation ON {SCHEMA}.c' || i "
                        f"       || ' USING ({chk_policy})'; "
                        "END LOOP; END $do$;"
                    )
                )
        await _load(conn, space)
    return round(time.perf_counter() - started, 1)


async def _load(conn: AsyncConnection, space: tuple[str, str]) -> None:
    """Route the corpus into whichever partitions hold it, and analyse only those.

    `ANALYZE` on the parent would touch every partition in one transaction and run out of the
    lock table — the same failure `_drop_tables` avoids from the other direction.
    `pg_relation_size > 0` rather than `reltuples`, which is -1 on a never-analysed table:
    that is every partition here, so the obvious filter would select all of them.
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
            f"INSERT INTO {SCHEMA}.chk (id, document_id, tenant_id, label_ids, text) "
            "SELECT a.chunk_id, c.document_id, a.tenant_id, a.label_ids, c.text "
            f"FROM {SCHEMA}.assign a JOIN chunks c ON c.id = a.chunk_id"
        )
    )
    await conn.execute(
        text(
            "DO $do$ DECLARE r record; BEGIN "
            "FOR r IN SELECT c.oid::regclass AS rel FROM pg_class c "
            "  JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"  WHERE n.nspname = '{SCHEMA}' AND c.relkind = 'r' "
            "  AND c.relname <> 'assign' AND pg_relation_size(c.oid) > 0 LOOP "
            "  EXECUTE 'ANALYZE ' || r.rel; END LOOP; END $do$;"
        )
    )


async def _probe_vector(owner: AsyncEngine, tenant: str) -> tuple[str, str, str]:
    """One real embedding, as the text a `halfvec` literal is written in.

    A real vector rather than a generated one, and one belonging to the tenant that will be
    queried: the arms are compared on the rows they return, and a query vector from another
    tenant's document would put the interesting rows out of reach of both.

    Read once per rung. The trap this avoids is named in `modulus_cost.py`: a harness that
    declared a `text` placeholder inside `CAST(... AS halfvec(1024))` timed its own cast and
    invented a 37x regression.
    """
    async with owner.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT embedding_half::text AS vec, embedding_model AS mdl, "
                    f"embedding_version AS ver FROM {SCHEMA}.emb "
                    "WHERE tenant_id = CAST(:t AS uuid) LIMIT 1"
                ),
                {"t": tenant},
            )
        ).one()
        await conn.rollback()
    return str(row.vec), str(row.mdl), str(row.ver)


# --- The arms ----------------------------------------------------------------------------


async def _plan_arm(conn: AsyncConnection, statement: str) -> dict[str, object]:
    """One statement, planned and executed `PLAN_REPEATS` times, with the plan kept.

    The plan is kept for every arm and not only the timing. `Subplans Removed`, the partitions
    named and the `One-Time Filter` are what say the query did the thing the number is being
    attributed to; three null results this week were indistinguishable from experiments that
    never ran, and one of them scored `Subplans Removed: 255` while moving the lock count not
    at all.
    """
    planning: list[float] = []
    execution: list[float] = []
    named: list[int] = []
    pruning: list[list[int]] = []
    plan: dict[str, Any] = {}
    for _ in range(PLAN_REPEATS):
        plan = _read_plan(await _explain(conn, statement))
        if plan["planning_ms"] is not None:
            planning.append(float(plan["planning_ms"]))
        if plan["execution_ms"] is not None:
            execution.append(float(plan["execution_ms"]))
        named.append(int(plan["partitions_in_plan"] or 0))
        removed = plan["subplans_removed"]
        pruning.append(list(removed) if isinstance(removed, list) else [])
    return {
        "planning_ms": _spread(planning),
        "execution_ms": _spread(execution),
        "planning_series_ms": [round(value, 3) for value in planning],
        "subplans_removed": plan.get("subplans_removed"),
        "appends": plan.get("appends"),
        "partitions_in_plan": plan.get("partitions_in_plan"),
        "partitions_in_plan_series": named,
        "pruning_was_stable": len({tuple(entry) for entry in pruning}) == 1,
        "policy_became_a_one_time_filter": plan.get("policy_became_a_one_time_filter"),
        "leaf_scans": plan.get("leaf_scans"),
    }


async def _plans(
    app: AsyncEngine, tenant: str, vector: str, space: tuple[str, str]
) -> dict[str, object]:
    """The three statement shapes, planned as the application role with policies in force.

    As the application role and not the owner, and that is the opposite of what
    `modulus_cost.py`'s lexical arms had to do. There the function is `SECURITY DEFINER`, so
    planning it under policies measured a query the product never runs — and ParadeDB refused
    it outright. Here the policies are the subject: a plan taken with RLS off would be a plan
    of a different query, and the `One-Time Filter` that carries the whole security argument
    would not appear in it at all.
    """
    vec, mdl, ver = _literal(vector), _literal(space[0]), _literal(space[1])
    arms: dict[str, object] = {}
    async with app.connect() as conn:
        await _context(conn, tenant)
        for prepared in _PREPARES.values():
            await conn.execute(text(prepared.format(schema=SCHEMA).strip()))
        arms["today"] = await _plan_arm(conn, f"EXECUTE p_today({vec}, {mdl}, {ver}, {WANTED})")
        arms["local"] = await _plan_arm(
            conn, f"EXECUTE p_local({_literal(tenant)}, {vec}, {mdl}, {ver}, {WANTED})"
        )
        arms["guc"] = await _plan_arm(conn, f"EXECUTE p_guc({vec}, {mdl}, {ver}, {WANTED})")
        await conn.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
        arms["local_forced_generic"] = await _plan_arm(
            conn, f"EXECUTE p_local({_literal(tenant)}, {vec}, {mdl}, {ver}, {WANTED})"
        )
        await conn.rollback()
    return arms


async def _calls(
    app: AsyncEngine, tenant: str, vector: str, space: tuple[str, str]
) -> dict[str, object]:
    """Each function's locks, wall clock and row count, charged to the application role.

    The locks are the number Bar 5 is about and they only exist inside the transaction that
    takes them. The wall clock is here because `EXPLAIN` on a call reports `Function Scan` and
    one number: the planning inside a plpgsql body is invisible to it, and the clock is the
    only instrument that sees the whole cost the product pays.

    A fresh connection per arm, so one arm's relation cache is not the next arm's head start.
    """
    out: dict[str, object] = {}
    for name in ("f_shipped", "f_local", "f_guc", "f_dynamic"):
        async with app.connect() as conn:
            await _context(conn, tenant)
            call = text(
                f"SELECT count(*) AS n FROM {SCHEMA}.{name}(CAST(:v AS halfvec(1024)), :m, :s, :w)"
            )
            params = {"v": vector, "m": space[0], "s": space[1], "w": WANTED}
            baseline = await _locks(conn)
            try:
                rows = int((await conn.execute(call, params)).scalar_one())
                held = await _locks(conn) - baseline
                timings: list[float] = []
                for _ in range(CALL_REPEATS):
                    started = time.perf_counter()
                    await conn.execute(call, params)
                    timings.append((time.perf_counter() - started) * 1000.0)
                out[name] = {
                    "locks": held,
                    "returned": rows,
                    "wall_ms": _spread(timings),
                    # Kept per execution and not only summarised, because the interesting
                    # thing in this series is a single spike rather than its shape. Postgres
                    # will build a candidate *generic* plan once it has planned custom five
                    # times, and building that candidate means planning the unpruned query in
                    # full. The spike is that plan, paid once per statement per backend, and a
                    # median smooths it away exactly where it is largest.
                    "wall_series_ms": [round(value, 3) for value in timings],
                }
            except Exception as exc:  # noqa: BLE001 - a refusal here is a result
                out[name] = {"error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"}
            await conn.rollback()
    return out


async def _equivalence(
    app: AsyncEngine, tenant: str, vector: str, space: tuple[str, str], labels: str = ""
) -> dict[str, object]:
    """Bar 2: the same chunk ids at the same ranks, and the scores to the bit.

    Compared per chunk rather than by taking a maximum over each side. Taking a maximum is
    what hid a score difference in an earlier run of `unpruned-plpgsql-pruning.sql`, where two
    arms with different scores had the same top score and looked identical.
    """
    out: dict[str, object] = {}
    async with app.connect() as conn:
        await _context(conn, tenant, labels)
        for name in ("f_local", "f_guc", "f_dynamic"):
            row = (
                await conn.execute(
                    text(
                        "WITH shipped AS ("
                        "  SELECT chunk_id, score, row_number() OVER "
                        "         (ORDER BY score DESC, chunk_id) AS rank "
                        f"  FROM {SCHEMA}.f_shipped(CAST(:v AS halfvec(1024)), :m, :s, :w)), "
                        "cand AS ("
                        "  SELECT chunk_id, score, row_number() OVER "
                        "         (ORDER BY score DESC, chunk_id) AS rank "
                        f"  FROM {SCHEMA}.{name}(CAST(:v AS halfvec(1024)), :m, :s, :w)) "
                        "SELECT (SELECT count(*) FROM shipped) AS shipped_rows, "
                        "       (SELECT count(*) FROM cand) AS candidate_rows, "
                        "       count(*) AS in_both, "
                        "       coalesce(max(abs(s.score - c.score)), 0) AS max_score_delta, "
                        "       count(*) FILTER (WHERE s.rank <> c.rank) AS at_a_different_rank "
                        "FROM shipped s JOIN cand c ON c.chunk_id = s.chunk_id"
                    ),
                    {"v": vector, "m": space[0], "s": space[1], "w": WANTED},
                )
            ).one()
            out[name] = {
                "shipped_rows": int(row.shipped_rows),
                "candidate_rows": int(row.candidate_rows),
                "in_both": int(row.in_both),
                "max_score_delta": float(row.max_score_delta),
                "rows_at_a_different_rank": int(row.at_a_different_rank),
            }
        await conn.rollback()
    return out


async def _isolation(app: AsyncEngine, vector: str, space: tuple[str, str]) -> dict[str, object]:
    """Bars 3 and 4: try to make the qualifier decide instead of the policy.

    The grid is the whole of Bar 3. `f_forced` takes the tenant as an argument, so every cell
    asks a session that is tenant *context* for the rows of tenant *requested*, and off the
    diagonal the local and the policy disagree by construction. Counted against `assign`,
    which carries no policy, so a leak cannot hide inside the arm being tested.

    Bar 4 is the second half, and it is the one a single-shot probe cannot see: nine calls on
    one backend under one tenant, then another tenant on the *same* backend, then the same
    again with a generic plan forced. If a folded constant ever survived into another tenant's
    call this is where it would show.
    """
    tenants = [_tenant(index) for index in range(1, TENANTS + 1)]
    params = {"v": vector, "m": space[0], "s": space[1], "w": WANTED}
    grid: list[dict[str, object]] = []
    off_diagonal = 0
    on_diagonal = 0
    async with app.connect() as conn:
        for context in tenants:
            await _context(conn, context)
            for requested in tenants:
                row = (
                    await conn.execute(
                        text(
                            "SELECT count(*) AS n, "
                            "       count(*) FILTER (WHERE a.tenant_id <> CAST(:c AS uuid)) "
                            "         AS foreign_rows "
                            f"FROM {SCHEMA}.f_forced(CAST(:r AS uuid), "
                            "     CAST(:v AS halfvec(1024)), :m, :s, :w) f "
                            f"JOIN {SCHEMA}.assign a ON a.chunk_id = f.chunk_id"
                        ),
                        {**params, "c": context, "r": requested},
                    )
                ).one()
                count, foreign = int(row.n), int(row.foreign_rows)
                if context == requested:
                    on_diagonal += count
                else:
                    off_diagonal += count
                    if count:
                        grid.append(
                            {
                                "context": context,
                                "requested": requested,
                                "rows": count,
                                "foreign_rows": foreign,
                            }
                        )
        await conn.rollback()

    # Bar 4, on one backend so the plan cache is the same one throughout.
    stale: list[dict[str, object]] = []
    async with app.connect() as conn:
        await _context(conn, _tenant(3))
        call = text(
            f"SELECT count(*) AS n, count(*) FILTER (WHERE a.tenant_id <> CAST(:c AS uuid)) "
            f"AS foreign_rows FROM {SCHEMA}.f_local(CAST(:v AS halfvec(1024)), :m, :s, :w) f "
            f"JOIN {SCHEMA}.assign a ON a.chunk_id = f.chunk_id"
        )
        for _ in range(9):
            await conn.execute(call, {**params, "c": _tenant(3)})
        for label, mode in (
            ("after nine calls as tenant 3, now tenant 5", None),
            ("the same with a generic plan forced", "force_generic_plan"),
        ):
            await _context(conn, _tenant(5))
            if mode:
                await conn.execute(text(f"SET LOCAL plan_cache_mode = {mode}"))
            row = (await conn.execute(call, {**params, "c": _tenant(5)})).one()
            stale.append(
                {"state": label, "rows": int(row.n), "foreign_rows": int(row.foreign_rows)}
            )
        await conn.rollback()

    # The failure mode is inverted on purpose: no tenant returns nothing, not everything.
    empty: dict[str, int] = {}
    async with app.connect() as conn:
        await _context(conn, "")
        for name in ("f_shipped", "f_local", "f_guc", "f_dynamic"):
            empty[name] = int(
                (
                    await conn.execute(
                        text(
                            f"SELECT count(*) FROM {SCHEMA}.{name}("
                            "CAST(:v AS halfvec(1024)), :m, :s, :w)"
                        ),
                        params,
                    )
                ).scalar_one()
            )
        await conn.rollback()

    return {
        "tenants": TENANTS,
        "cells": TENANTS * TENANTS,
        "rows_on_the_diagonal": on_diagonal,
        "rows_off_the_diagonal": off_diagonal,
        "cells_that_returned_anything_off_the_diagonal": grid,
        "across_the_plan_cache": stale,
        "rows_with_no_tenant_in_the_session": empty,
    }


async def _owner_side(owner: AsyncEngine, vector: str, space: tuple[str, str]) -> dict[str, object]:
    """The one path where the qualifier really is the thing that decides.

    `owner_session()` and `platform_session()` bypass RLS, so on those paths there is no policy
    for the qualifier to be redundant with. This is not hypothetical and it is not a leak — it
    is the direction that matters, and the direction is printed here rather than argued.
    `distinct_tenants` says it: the shipped form sees every tenant, the candidate sees the one
    in the session GUC, and a narrowing is not a widening.

    The second half is the cost of that: with no tenant set at all the candidate returns
    nothing, because its guard clause refuses a NULL tenant. A function shaped like this is
    therefore not a drop-in for an owner-side caller — which is a fact about where it may be
    used, and belongs in the report rather than in a footnote.
    """
    params = {"v": vector, "m": space[0], "s": space[1], "w": WANTED}
    out: dict[str, object] = {}
    async with owner.connect() as conn:
        # The same session settings the application gets. The first version of this section
        # omitted `hnsw.iterative_scan` and the row counts came back at 40 of 50 rather than
        # 50 — the post-filter loss `search.dense()` documents and `tenant-scale.json`
        # measured, arriving here because the tenant qualifier is a filter over the graph like
        # any other. It changed nothing about `distinct_tenants`, which is what this section
        # is for, but a row count that is wrong for an understood reason is still a row count
        # a reader has to be told about, and the cheaper fix is to stop producing it.
        await conn.execute(text(f"SET hnsw.iterative_scan = {ITERATIVE_SCAN}"))
        await conn.execute(text("SET max_parallel_workers_per_gather = 0"))
        for label, tenant in (("with tenant 3 in the session", _tenant(3)), ("with none", "")):
            await conn.execute(
                text(
                    "SELECT set_config('zenith.tenant_id', :t, true), "
                    "       set_config('zenith.label_ids', '', true)"
                ),
                {"t": tenant},
            )
            for name in ("f_shipped", "f_local"):
                row = (
                    await conn.execute(
                        text(
                            "SELECT count(*) AS n, count(DISTINCT a.tenant_id) AS tenants "
                            f"FROM {SCHEMA}.{name}(CAST(:v AS halfvec(1024)), :m, :s, :w) f "
                            f"JOIN {SCHEMA}.assign a ON a.chunk_id = f.chunk_id"
                        ),
                        params,
                    )
                ).one()
                out[f"{name}, {label}"] = {
                    "rows": int(row.n),
                    "distinct_tenants": int(row.tenants),
                }
        await conn.rollback()
    return out


# --- One rung ----------------------------------------------------------------------------


async def _rung(
    owner: AsyncEngine, app: AsyncEngine, modulus: int | None, space: tuple[str, str]
) -> dict[str, object]:
    built = await _build(owner, modulus, space)
    tenant = _tenant(3)
    vector, model, version = await _probe_vector(owner, tenant)
    space = (model, version)
    rung: dict[str, object] = {
        "modulus": modulus,
        "built_seconds": built,
        "plans": await _plans(app, tenant, vector, space),
        "calls": await _calls(app, tenant, vector, space),
        "equivalence": await _equivalence(app, tenant, vector, space),
        "equivalence_under_one_label": await _equivalence(app, tenant, vector, space, LABEL_A),
        "isolation": await _isolation(app, vector, space),
        "owner_side": await _owner_side(owner, vector, space),
    }
    return rung


# --- The verdict -------------------------------------------------------------------------


def _bars(rungs: list[dict[str, Any]]) -> dict[str, object]:
    """The five bars, evaluated from the stored report rather than at measurement time.

    Read off what was written so a rung measured in an earlier pass is judged by the same rule
    as one measured in this pass — and so that a bar cannot be quietly relaxed by the code that
    also decides whether it passed.
    """
    partitioned = [rung for rung in rungs if rung.get("modulus")]
    failures: list[str] = []

    for rung in partitioned:
        modulus = int(rung["modulus"])
        plans = rung.get("plans", {})
        today = plans.get("today", {})
        local = plans.get("local", {})
        if sorted(today.get("subplans_removed") or []) != [modulus - 1, modulus - 1]:
            failures.append(
                f"modulus {modulus}: the shipped arm removed "
                f"{today.get('subplans_removed')}, not [{modulus - 1}, {modulus - 1}]"
            )
        if int(local.get("partitions_in_plan") or 0) != PARTITIONS_WHEN_PRUNED:
            failures.append(
                f"modulus {modulus}: the pruned arm named "
                f"{local.get('partitions_in_plan')} partitions, not {PARTITIONS_WHEN_PRUNED}"
            )
        if local.get("appends"):
            failures.append(f"modulus {modulus}: the pruned arm still has an Append")

    bar1 = not failures

    bar2 = True
    for rung in rungs:
        for section in ("equivalence", "equivalence_under_one_label"):
            arm = rung.get(section, {}).get("f_local", {})
            if not arm:
                bar2 = False
                continue
            # `or 1.0` here read a passing delta of 0.0 as a missing one and failed the bar
            # against a set of rows that matched exactly. Left as a comment rather than
            # quietly corrected: a bar that fails for the wrong reason is as useless as one
            # that passes for the wrong reason, and this one did both in the same expression.
            delta = arm.get("max_score_delta")
            if (
                arm.get("in_both") != arm.get("shipped_rows")
                or arm.get("candidate_rows") != arm.get("shipped_rows")
                or delta is None
                or float(delta) != 0.0
                or arm.get("rows_at_a_different_rank")
            ):
                bar2 = False
                failures.append(f"modulus {rung.get('modulus')}: {section} differs — {arm}")

    bar3 = True
    bar4 = True
    for rung in rungs:
        isolation = rung.get("isolation", {})
        if isolation.get("rows_off_the_diagonal"):
            bar3 = False
            failures.append(
                f"modulus {rung.get('modulus')}: "
                f"{isolation['rows_off_the_diagonal']} rows off the diagonal"
            )
        if not isolation.get("rows_on_the_diagonal"):
            bar3 = False
            failures.append(f"modulus {rung.get('modulus')}: the diagonal returned nothing")
        for entry in isolation.get("across_the_plan_cache", []):
            if entry.get("foreign_rows") or not entry.get("rows"):
                bar4 = False
                failures.append(f"modulus {rung.get('modulus')}: plan cache — {entry}")
        if any(isolation.get("rows_with_no_tenant_in_the_session", {}).values()):
            bar3 = False
            failures.append(
                f"modulus {rung.get('modulus')}: an arm answered with no tenant in the session"
            )

    bar5 = True
    for rung in partitioned:
        held = rung.get("calls", {}).get("f_local", {}).get("locks")
        if held is None or int(held) > LOCK_CEILING:
            bar5 = False
            failures.append(f"modulus {rung.get('modulus')}: the pruned arm held {held} locks")

    return {
        "bar_1_the_arms_are_different_plans": bar1,
        "bar_2_the_rows_are_identical": bar2,
        "bar_3_the_qualifier_never_decides": bar3,
        "bar_4_isolation_survives_the_plan_cache": bar4,
        "bar_5_the_locks_stop_growing": bar5,
        "all_five": bar1 and bar2 and bar3 and bar4 and bar5,
        "failures": failures,
    }


def _curve(rungs: list[dict[str, Any]]) -> list[dict[str, object]]:
    """One row per rung, the four numbers a reader wants side by side."""
    rows: list[dict[str, object]] = []
    for rung in rungs:
        plans = rung.get("plans", {})
        calls = rung.get("calls", {})
        rows.append(
            {
                "modulus": rung.get("modulus"),
                "today_planning_ms": (plans.get("today", {}).get("planning_ms") or {}).get(
                    "median"
                ),
                "local_planning_ms": (plans.get("local", {}).get("planning_ms") or {}).get(
                    "median"
                ),
                "guc_planning_ms": (plans.get("guc", {}).get("planning_ms") or {}).get("median"),
                "local_generic_planning_ms": (
                    plans.get("local_forced_generic", {}).get("planning_ms") or {}
                ).get("median"),
                "today_partitions_in_plan": plans.get("today", {}).get("partitions_in_plan"),
                "local_partitions_in_plan": plans.get("local", {}).get("partitions_in_plan"),
                "today_locks": calls.get("f_shipped", {}).get("locks"),
                "local_locks": calls.get("f_local", {}).get("locks"),
                "guc_locks": calls.get("f_guc", {}).get("locks"),
                "dynamic_locks": calls.get("f_dynamic", {}).get("locks"),
                "today_wall_ms": (calls.get("f_shipped", {}).get("wall_ms") or {}).get("median"),
                "local_wall_ms": (calls.get("f_local", {}).get("wall_ms") or {}).get("median"),
                "dynamic_wall_ms": (calls.get("f_dynamic", {}).get("wall_ms") or {}).get("median"),
            }
        )
    return rows


async def _verify(owner: AsyncEngine) -> dict[str, object]:
    """The corpus is what it was, and the scratch schema is gone.

    Both are asked rather than assumed. A sweep that silently wrote to `chunks` would be
    indistinguishable from one that did not, and a schema left behind is the failure a
    neighbouring branch produced with 2,075 relations.
    """
    async with owner.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        chunks = int((await conn.execute(text("SELECT count(*) FROM chunks"))).scalar_one())
        embeddings = int(
            (await conn.execute(text("SELECT count(*) FROM chunk_embeddings"))).scalar_one()
        )
        left = int(
            (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_class c JOIN pg_namespace n "
                        "ON n.oid = c.relnamespace WHERE n.nspname = :s"
                    ),
                    {"s": SCHEMA},
                )
            ).scalar_one()
        )
        await conn.rollback()
    return {"chunks": chunks, "chunk_embeddings": embeddings, "relations_left_behind": left}


def _engines() -> tuple[AsyncEngine, AsyncEngine]:
    """A fresh owner and application engine, recycled between rungs.

    A backend's relation cache is per-connection and is never given back. `modulus_cost.py`
    lost a cluster to that: after several rungs the same two backends held catalogue entries
    for close to ten thousand relations and the OOM killer took one of them mid-ladder.
    Disposing between rungs is also the more honest measurement — every rung starts cold
    rather than inheriting the one before it.
    """
    return (
        create_async_engine(settings.database_owner_url, pool_size=2, max_overflow=0),
        create_async_engine(settings.database_url, pool_size=2, max_overflow=0),
    )


async def _space(owner: AsyncEngine) -> tuple[str, str]:
    async with owner.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        row = (
            await conn.execute(
                text(
                    "SELECT embedding_model AS m, embedding_version AS v, count(*) AS n "
                    "FROM chunk_embeddings GROUP BY 1, 2 ORDER BY n DESC LIMIT 1"
                )
            )
        ).one()
        await conn.rollback()
    return str(row.m), str(row.v)


async def _run(rungs: list[int | None]) -> int:
    owner, app = _engines()
    report: dict[str, Any] = {
        "question": (
            "Can the dense retrieval query prune at plan time instead of executor startup, "
            "without weakening row-level security?"
        ),
        "schema": SCHEMA,
        "wanted": WANTED,
        "tenants": TENANTS,
        "plan_repeats": PLAN_REPEATS,
        "lock_ceiling": LOCK_CEILING,
        "rungs": [],
    }
    try:
        space = await _space(owner)
        report["embedding_space"] = {"model": space[0], "version": space[1]}
        await _drop(owner)
        report["assign"] = await _assign(owner, space)
        for modulus in rungs:
            print(f"--- modulus {modulus or 'unpartitioned'}", flush=True)
            try:
                report["rungs"].append(await _rung(owner, app, modulus, space))
            except Exception as exc:  # noqa: BLE001 - a rung that fails is a reading
                report["rungs"].append(
                    {
                        "modulus": modulus,
                        "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}",
                    }
                )
            finally:
                await _teardown(owner)
                await owner.dispose()
                await app.dispose()
                owner, app = _engines()
    finally:
        with contextlib.suppress(Exception):
            await _drop(owner)
        report["verified"] = await _verify(owner)
        await owner.dispose()
        await app.dispose()

    report["curve"] = _curve(report["rungs"])
    report["bars"] = _bars(report["rungs"])
    REPORT.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n")

    print(f"\nWrote {REPORT}")
    for row in report["curve"]:
        print(
            f"  modulus {str(row['modulus'] or 'none'):>4}  "
            f"today {row['today_planning_ms']} ms / {row['today_locks']} locks   "
            f"local {row['local_planning_ms']} ms / {row['local_locks']} locks"
        )
    bars: dict[str, Any] = report["bars"]
    failures: list[str] = bars["failures"]
    for key, value in bars.items():
        if key != "failures":
            print(f"  {key}: {value}")
    for failure in failures:
        print(f"  ! {failure}")
    return 0 if bars["all_five"] else 1


COMMAND = "dense-plan-time"
USAGE = "dense-plan-time [--partitions 32,64,128,256]"


def cli(argv: list[str]) -> int:
    rungs = list(RUNGS)
    for index, argument in enumerate(argv):
        if argument == "--partitions" and index + 1 < len(argv):
            rungs = [None if part == "none" else int(part) for part in argv[index + 1].split(",")]
    return asyncio.run(_run(rungs))


def run() -> int:
    return cli(sys.argv[1:])
