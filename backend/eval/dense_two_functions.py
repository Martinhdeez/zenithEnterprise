# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""If the disjunct is what breaks it, do not write a disjunct: two functions instead of one.

`dense-plan-time-pruning.sql` section 5f is the file this one continues, and its finding is the
premise here. Lifting `zenith_current_tenant()` into a plpgsql local inside a `SECURITY INVOKER`
function makes the *unscoped* dense query prune at plan time — one partition per relation, no
`Append`, 16 locks against 233, planning 0.16-0.18 ms against 1.31 ms, identical rows — and it
degrades gracefully under a generic plan, losing the pruning but keeping the HNSW index.

The *scoped* form does not. `search.dense()` takes an optional document scope, and a plpgsql
signature cannot be built by concatenation the way `scoped()` builds the Python `WHERE`, so the
filter became unconditional behind a NULL guard: `AND (docs IS NULL OR c.document_id =
ANY(docs))`. Under a generic plan — which `dense-plan-time.json`'s own `planning_series_ms`
shows Postgres reaching unforced, on the fifth timed call, at every modulus — that form replaces
the ordered `Index Scan` with a `Seq Scan` over the whole partition, a `Hash Join` and a
top-N `Sort`, and the HNSW index is not named anywhere in the plan. 5f-7 isolates the cause:
the plans are identical whether `docs` is a scope or NULL, so it is not the runtime value —
a generic plan cannot see that — it is the presence of the disjunct in the statement's *shape*.

## The hypothesis this file tries to break

Ship two functions rather than one, and let the caller pick:

  `candidate(q, mdl, ver, want)`             no scope, no `docs`, no `OR`
  `candidate_scoped(q, mdl, ver, want, docs)`  `docs` never NULL, so the predicate is a bare
                                               `c.document_id = ANY(docs)` and there is no
                                               disjunct to cost

`search.py` already chooses whether to write the scope clause; it would choose which function to
call instead. Nothing has to be built by concatenation, because nothing is built.

**The way this most likely fails is not the disjunct at all.** `= ANY($6)` on an array parameter
may defeat ordered index retrieval on its own: a generic plan cannot see the array's selectivity
either, and the join strategy the `LIMIT` pushdown depends on is chosen once for the whole
statement. If that is what happened in 5f-6, removing the `OR` buys nothing and the answer is
"ship the unscoped function only". That is the outcome this file is built to detect, which is
why `shipped_scoped` — the statement `search.dense()` sends today, generic plan and all — is an
arm here rather than a footnote. A candidate that loses the index is only a regression if the
code it replaces kept it.

## The bars, written before the run

A failed bar stays in this file rather than being rewritten.

**Bar 1 — the controls have to reproduce.** At every rung, under a forced generic plan, the
unscoped `local` arm must still name the HNSW index and the `scoped_or` arm must not. These are
5f-8 and 5f-6 repeated on this harness's schema. If they do not come back the way the SQL file
recorded them, this run measured something else and no other bar in it means anything. This bar
is first because three null results this week were indistinguishable from experiments that never
ran.

**Bar 2 — the deciding question.** Under a forced generic plan, `scoped_bare` must name the HNSW
index in its plan. Not "must be fast": named. A plan that reads the whole partition sequentially
and sorts is the failure ADR 0002's dense half exists to avoid, and at this corpus's 1,694 rows
per tenant it is *cheap* — the timing cannot see it and only the plan can.

**Bar 3 — plan-time pruning, under a custom plan.** `scoped_bare` must name exactly
`PARTITIONS_WHEN_PRUNED` partitions, carry no `Append`, report no `Subplans Removed`, fold the
policy into a `One-Time Filter`, and name the HNSW index. This is Bar 1 of the SQL file, asked
of the new shape.

**Bar 4 — the rows have to be identical, to the bit.** Against the literal statement
`search.dense()` sends when scoped — not against the unscoped arm filtered by hand. The same 50
chunk ids at the same 50 ranks, `max_score_delta` exactly `0.0`, and `rows_at_a_different_rank`
zero. Anything else and the arm is not a candidate however fast it is.

**Bar 5 — the qualifier must never be the thing that decides.** The full
`TENANTS` x `TENANTS` grid on the scoped shape, each cell asking a session that is tenant
*context* for tenant *requested*'s rows **with tenant *requested*'s own documents as the scope**,
so that both the qualifier and the scope are foreign off the diagonal. Rows on the diagonal,
nothing off it, counted against `assign`, which carries no policy. Plus: a session that is
tenant 3, asking for tenant 3, with another tenant's documents as the scope, must return
nothing — a document scope is not an access grant.

**Bar 6 — the two functions must not be able to drift apart silently.** `scoped_bare` given
*every* document the session tenant owns must return exactly what the unscoped `local` returns:
same ids, same ranks, `max_score_delta` `0.0`. Two functions is two things to keep in step, and
this is the check that would fail the day one of them stopped matching the other. It is a
measurement of the risk rather than an opinion about it.

**Bar 7 — locks.** `scoped_bare` must hold no more than `LOCK_CEILING` locks. The saving being
claimed is a lock saving before it is a timing saving, and a shape that prunes in the plan and
locks 233 relations anyway has not pruned.

## What is deliberately not a bar

`shipped_scoped` under a forced generic plan is recorded and judged by nobody. It is the
baseline: whatever it does is what production does today, and it is the only thing that turns
"`scoped_bare` loses the index" into either a regression or a wash. Bar 2 is still judged in
absolute terms — a plan that reads a whole partition sequentially is a bad plan at production
partition sizes whatever the plan beside it does — and `verdict` in the report is what says
whether failing it costs anything against today.

The promotion watch is not a bar either. `dense-plan-time.json`'s planning spike on the sixth
call was read as Postgres *reaching* a generic plan unforced, and building a candidate generic
plan and using one are different events: the plancache prices the candidate and goes on planning
custom if the custom plans are cheaper. `_promotion` runs forty unforced executions of each arm
on one backend and reads the plan every time, so whether the forced arms are a forecast or a
bound is a reading rather than an inference.

## The installation these readings come from

Recorded per rung in `environment`, and not as decoration. This branch's measurements straddled
a deploy: the shared installation went from migration 0025 to 0026, `chunks` and
`chunk_embeddings` became partitioned in `public`, and `max_locks_per_transaction` went from 64
to 2,560 when the container was recreated — 6,400 cluster lock slots to 256,000. A lock count
from one side is not comparable with one from the other. Every figure in
`dense-two-functions.json` was taken after that boundary; the arms measured before it were
discarded rather than mixed in.

Built on `dense_plan_time`'s schema builder, tenants, index set and plan reader — the same
corpus placed the same way, so the numbers here sit beside that file's rather than near them.
Its `SCHEMA` is rebound below so the two can run at the same time. Read-only with respect to the
corpus: everything is inside `zenith_densetwo`, dropped in a `finally`.

    cd backend && python -m eval dense-two-functions [--partitions 32,256]
"""

import asyncio
import contextlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from eval import dense_plan_time as dpt

#: A schema of this file's own, so a `dense-plan-time` sweep can run at the same time. Rebound
#: on the imported module because every builder there reads the module global at call time —
#: the alternative is a second copy of a schema builder that has already been debugged twice.
dpt.SCHEMA = "zenith_densetwo"
SCHEMA = dpt.SCHEMA

REPORT = Path(__file__).parent / "dense-two-functions.json"

#: 32 is the modulus `dense-plan-time-pruning.sql` exhibits its plans at, so the plans here can
#: be read against those line for line. 256 is the rung `dense-plan-time.json` quotes its
#: headline lock and planning figures from. Partition *size* is set by the tenant count rather
#: than the modulus — eight synthetic tenants over 13,549 passages is roughly 1,694 rows in
#: whichever partition a tenant hashes to, at 32 and at 256 alike — so the two rungs differ in
#: how many empty partitions the planner has to open and in nothing else.
RUNGS: tuple[int, ...] = (32, 256)

#: Documents per tenant in the scope. Three, as the SQL file's 5f uses: wide enough that 50 rows
#: can still be filled, narrow enough to be a real restriction.
SCOPE_DOCUMENTS = 3

WANTED = dpt.WANTED
TENANTS = dpt.TENANTS
PARTITIONS_WHEN_PRUNED = dpt.PARTITIONS_WHEN_PRUNED
LOCK_CEILING = dpt.LOCK_CEILING
PLAN_REPEATS = dpt.PLAN_REPEATS


# --- Reading a plan for the one thing that decides ----------------------------------------

#: Every index a scan node names. The HNSW index is created unnamed on the parent, so Postgres
#: propagates `e17_embedding_half_idx` to each partition: the column name is the stable part and
#: the partition number is not.
_USING = re.compile(r"(?:Index Scan|Index Only Scan|Bitmap Index Scan) using (\w+)")
_SEQ_SCAN = re.compile(r"Seq Scan on (\w+)")
_HASH_JOIN = re.compile(r"Hash Join")
_SORT_METHOD = re.compile(r"Sort Method: ([\w\- ]+)")


def _flags(lines: list[str]) -> dict[str, Any]:
    """Whether the HNSW index is in this plan at all, and what stands where it was.

    `dense_plan_time._read_plan` answers the partitioning questions and truncates the scan lines
    to four, which is the right shape for a pruning ladder and the wrong one here: the arm this
    file is trying to break loses the index *and* keeps a perfectly ordinary-looking execution
    time, so the index name is the only reading that separates a pass from a failure.
    """
    joined = "\n".join(lines)
    indexes = sorted(set(_USING.findall(joined)))
    return {
        "hnsw_index_in_plan": any("embedding_half" in name for name in indexes),
        "indexes_used": indexes,
        "seq_scans": sorted(set(_SEQ_SCAN.findall(joined))),
        "hash_join": bool(_HASH_JOIN.search(joined)),
        "sort_methods": sorted(set(_SORT_METHOD.findall(joined))),
    }


def _plan_text(lines: list[str]) -> list[str]:
    """The plan as written, with the vector literal cut out of it.

    A `halfvec(1024)` literal is thirteen kilobytes and appears on every `Order By` line. The
    report is meant to be read, and an unreadable report is a report nobody checks the claim
    against.
    """
    return [re.sub(r"'\[[^\]]*\]'", "'[...]'", line)[:220] for line in lines]


async def _arm(conn: AsyncConnection, statement: str) -> dict[str, Any]:
    """One statement, planned and executed `PLAN_REPEATS` times, with every plan read.

    The series is the point, not the summary. Postgres builds a candidate generic plan once it
    has planned custom five times, and `dense-plan-time.json` already recorded that promotion
    happening unforced on the sixth reading of this very query. A summary of eleven executions
    where the sixth changed shape reports the shape of neither, so `hnsw_series` and
    `partitions_series` are kept per execution and the plan text is kept for the first and the
    last.
    """
    planning: list[float] = []
    execution: list[float] = []
    hnsw: list[bool] = []
    partitions: list[int] = []
    removed: list[list[int]] = []
    first: list[str] = []
    last: list[str] = []
    flags: dict[str, Any] = {}
    plan: dict[str, Any] = {}

    for index in range(PLAN_REPEATS):
        lines = await dpt._explain(conn, statement)
        plan = dpt._read_plan(lines)
        flags = _flags(lines)
        if index == 0:
            first = _plan_text(lines)
        last = _plan_text(lines)
        if plan["planning_ms"] is not None:
            planning.append(float(plan["planning_ms"]))
        if plan["execution_ms"] is not None:
            execution.append(float(plan["execution_ms"]))
        hnsw.append(bool(flags["hnsw_index_in_plan"]))
        partitions.append(int(plan["partitions_in_plan"] or 0))
        entry = plan["subplans_removed"]
        removed.append(list(entry) if isinstance(entry, list) else [])

    return {
        "planning_ms": dpt._spread(planning),
        "execution_ms": dpt._spread(execution),
        "planning_series_ms": [round(value, 3) for value in planning],
        "hnsw_index_in_plan": all(hnsw),
        "hnsw_index_series": hnsw,
        "hnsw_lost_at_execution": (hnsw.index(False) + 1) if False in hnsw else None,
        "partitions_in_plan": plan.get("partitions_in_plan"),
        "partitions_series": partitions,
        "subplans_removed": plan.get("subplans_removed"),
        "appends": plan.get("appends"),
        "pruning_was_stable": len({tuple(entry) for entry in removed}) == 1,
        "policy_became_a_one_time_filter": plan.get("policy_became_a_one_time_filter"),
        "indexes_used": flags.get("indexes_used"),
        "seq_scans": flags.get("seq_scans"),
        "hash_join": flags.get("hash_join"),
        "sort_methods": flags.get("sort_methods"),
        "plan_first_execution": first,
        "plan_last_execution": last,
    }


# --- The arms this file adds ---------------------------------------------------------------

#: Four functions on top of `dense_plan_time`'s five, all `SECURITY INVOKER`, all running under
#: the caller's policies.
#:
#: `f_shipped_scoped` is `search.dense()` with a document scope and nothing else: no tenant
#: qualifier, exactly as `scoped()` writes it today. It is the baseline for the scoped case and
#: the only thing that can say whether losing the HNSW index would be a regression or a wash.
#:
#: `f_scoped_or` is the shape section 5f measured and rejected — the filter unconditional behind
#: a NULL guard. It is here as a control: if it does not lose the index on this harness the way
#: it lost it in the SQL file, nothing else measured here can be trusted.
#:
#: `f_scoped_bare` is the hypothesis. `docs` is never NULL, because the caller picks this
#: function only when it has a scope, so the predicate is a bare `= ANY(docs)`. It is
#: `f_scoped_or` with the disjunct deleted and nothing else changed.
#:
#: `f_forced_scoped` is not a candidate for anything. It takes the tenant as an argument so Bar 5
#: can make the qualifier, the scope and the policy disagree on purpose.
_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION {schema}.f_shipped_scoped(
  q halfvec(1024), mdl text, ver text, want int, docs uuid[])
RETURNS TABLE(chunk_id uuid, score double precision)
LANGUAGE plpgsql STABLE SECURITY INVOKER
AS $body$
BEGIN
  RETURN QUERY
  SELECT c.id, 1 - (e.embedding_half <=> q)
  FROM {schema}.emb e
  JOIN {schema}.chk c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id
  WHERE e.embedding_model = mdl AND e.embedding_version = ver
    AND c.document_id = ANY(docs)
  ORDER BY e.embedding_half <=> q
  LIMIT want;
END;
$body$;
CREATE OR REPLACE FUNCTION {schema}.f_scoped_or(
  q halfvec(1024), mdl text, ver text, want int, docs uuid[])
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
    AND (docs IS NULL OR c.document_id = ANY(docs))
  ORDER BY e.embedding_half <=> q
  LIMIT want;
END;
$body$;
CREATE OR REPLACE FUNCTION {schema}.f_scoped_bare(
  q halfvec(1024), mdl text, ver text, want int, docs uuid[])
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
    AND c.document_id = ANY(docs)
  ORDER BY e.embedding_half <=> q
  LIMIT want;
END;
$body$;
CREATE OR REPLACE FUNCTION {schema}.f_forced_scoped(
  v_tenant uuid, q halfvec(1024), mdl text, ver text, want int, docs uuid[])
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
    AND c.document_id = ANY(docs)
  ORDER BY e.embedding_half <=> q
  LIMIT want;
END;
$body$;
"""

_GRANT_DDL = """
GRANT EXECUTE ON FUNCTION
  {schema}.f_shipped_scoped(halfvec(1024), text, text, int, uuid[]),
  {schema}.f_scoped_or(halfvec(1024), text, text, int, uuid[]),
  {schema}.f_scoped_bare(halfvec(1024), text, text, int, uuid[]),
  {schema}.f_forced_scoped(uuid, halfvec(1024), text, text, int, uuid[])
TO zenith_app
"""

#: `EXPLAIN` on a call does not descend into a plpgsql body — it reports `Function Scan` and one
#: number — so the plans are exhibited through `PREPARE`/`EXECUTE`, as `dense_plan_time` and
#: `dense-plan-time-pruning.sql` both do. A plpgsql statement *is* an SPI prepared plan whose
#: locals are its parameters, and it obeys `plan_cache_mode` exactly as these do. The functions
#: above are still what the equivalence, lock and isolation sections call.
#:
#: `p_scoped_bare` and `p_scoped_or` differ by the disjunct and by nothing else — same parameter
#: list, same positions, same order — so the difference between their plans is the disjunct or
#: it is noise.
_PREPARES = {
    "shipped_scoped": """
PREPARE p_shipped_scoped(halfvec(1024), text, text, int, uuid[]) AS
SELECT c.id, 1 - (e.embedding_half <=> $1) AS score
FROM {schema}.emb e
JOIN {schema}.chk c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id
WHERE e.embedding_model = $2 AND e.embedding_version = $3
  AND c.document_id = ANY($5)
ORDER BY e.embedding_half <=> $1 LIMIT $4
""",
    "scoped_or": """
PREPARE p_scoped_or(uuid, halfvec(1024), text, text, int, uuid[]) AS
SELECT c.id, 1 - (e.embedding_half <=> $2) AS score
FROM {schema}.emb e
JOIN {schema}.chk c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id
WHERE e.tenant_id = $1 AND e.embedding_model = $3 AND e.embedding_version = $4
  AND ($6 IS NULL OR c.document_id = ANY($6))
ORDER BY e.embedding_half <=> $2 LIMIT $5
""",
    "scoped_bare": """
PREPARE p_scoped_bare(uuid, halfvec(1024), text, text, int, uuid[]) AS
SELECT c.id, 1 - (e.embedding_half <=> $2) AS score
FROM {schema}.emb e
JOIN {schema}.chk c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id
WHERE e.tenant_id = $1 AND e.embedding_model = $3 AND e.embedding_version = $4
  AND c.document_id = ANY($6)
ORDER BY e.embedding_half <=> $2 LIMIT $5
""",
}


async def _functions(owner: AsyncEngine) -> None:
    async with owner.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        for body in _FUNCTION_DDL.format(schema=SCHEMA).split("$body$;"):
            if body.strip():
                await conn.execute(text(body + "$body$;"))
        await conn.execute(text(_GRANT_DDL.format(schema=SCHEMA)))


async def _scopes(owner: AsyncEngine) -> dict[str, list[str]]:
    """Each tenant's `SCOPE_DOCUMENTS` largest documents, read as the owner outside RLS.

    Per tenant rather than once, because Bar 5's grid needs the *requested* tenant's own
    documents in the scope. A grid that asked every cell for tenant 3's documents would find
    nothing off the diagonal for a reason that has nothing to do with isolation, and would
    record that as a pass.
    """
    out: dict[str, list[str]] = {}
    async with owner.connect() as conn:
        for index in range(1, TENANTS + 1):
            tenant = dpt._tenant(index)
            rows = list(
                await conn.execute(
                    text(
                        "SELECT document_id::text AS d, count(*) AS n "
                        f"FROM {SCHEMA}.chk WHERE tenant_id = CAST(:t AS uuid) "
                        "GROUP BY 1 ORDER BY n DESC, 1 LIMIT :k"
                    ),
                    {"t": tenant, "k": SCOPE_DOCUMENTS},
                )
            )
            out[tenant] = [str(row.d) for row in rows]
        await conn.rollback()
    return out


async def _all_documents(owner: AsyncEngine, tenant: str) -> list[str]:
    """Every document the tenant owns — Bar 6's scope, where the two functions must agree."""
    async with owner.connect() as conn:
        rows = list(
            await conn.execute(
                text(
                    f"SELECT DISTINCT document_id::text AS d FROM {SCHEMA}.chk "
                    "WHERE tenant_id = CAST(:t AS uuid)"
                ),
                {"t": tenant},
            )
        )
        await conn.rollback()
    return [str(row.d) for row in rows]


def _array(values: list[str]) -> str:
    """A uuid array as a SQL literal, for `EXECUTE`, which takes expressions and not parameters.

    Every value that reaches here was read out of the scratch schema this file built, and the
    doubling is here so that stays true if someone later passes something that was not.
    """
    inner = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    return f"ARRAY[{inner}]::uuid[]"


async def _plans(
    app: AsyncEngine,
    tenant: str,
    vector: str,
    space: tuple[str, str],
    scope: list[str],
) -> dict[str, object]:
    """Every arm, custom and forced-generic, as the application role with policies in force.

    As the application role and not the owner, for `dense_plan_time`'s reason: the policies are
    the subject here, and the `One-Time Filter` that carries the whole security argument does not
    appear in a plan taken with RLS off.

    The forced arms take a fresh connection each. Forcing the setting after eleven custom
    executions on the same backend measures a statement that has already been promoted once, and
    the question is what the plan looks like when the parameter is unknown from the first
    execution rather than from the twelfth.
    """
    vec, mdl, ver = dpt._literal(vector), dpt._literal(space[0]), dpt._literal(space[1])
    tid, docs = dpt._literal(tenant), _array(scope)
    calls = {
        # The unscoped pair, from `dense_plan_time`'s own prepares: `today` is what production
        # sends with no scope and `local` is the technique that already passed.
        "today": f"EXECUTE p_today({vec}, {mdl}, {ver}, {WANTED})",
        "local": f"EXECUTE p_local({tid}, {vec}, {mdl}, {ver}, {WANTED})",
        # The scoped three. `shipped_scoped` is the baseline, `scoped_or` the control that must
        # fail, `scoped_bare` the hypothesis.
        "shipped_scoped": f"EXECUTE p_shipped_scoped({vec}, {mdl}, {ver}, {WANTED}, {docs})",
        "scoped_or": f"EXECUTE p_scoped_or({tid}, {vec}, {mdl}, {ver}, {WANTED}, {docs})",
        "scoped_bare": f"EXECUTE p_scoped_bare({tid}, {vec}, {mdl}, {ver}, {WANTED}, {docs})",
    }
    prepares = [
        statement.format(schema=SCHEMA).strip()
        for statement in list(dpt._PREPARES.values()) + list(_PREPARES.values())
    ]

    arms: dict[str, object] = {}
    async with app.connect() as conn:
        await dpt._context(conn, tenant)
        for statement in prepares:
            await conn.execute(text(statement))
        for name, call in calls.items():
            arms[name] = await _arm(conn, call)
        await conn.rollback()

    for name, call in calls.items():
        async with app.connect() as conn:
            await dpt._context(conn, tenant)
            for statement in prepares:
                await conn.execute(text(statement))
            await conn.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
            arms[f"{name}_forced_generic"] = await _arm(conn, call)
            await conn.rollback()

    return arms


async def _locks(
    app: AsyncEngine,
    tenant: str,
    vector: str,
    space: tuple[str, str],
    scope: list[str],
) -> dict[str, object]:
    """What each function charges the caller, counted inside the transaction that takes them.

    A fresh connection per arm, so one arm's relation cache is not the next arm's head start.
    The lock table is cluster-wide and shared with whatever else is running on this machine, so
    this is a difference between two readings seconds apart rather than an absolute.
    """
    out: dict[str, object] = {}
    unscoped = ("f_shipped", "f_local")
    scoped = ("f_shipped_scoped", "f_scoped_or", "f_scoped_bare")
    for name in unscoped + scoped:
        async with app.connect() as conn:
            await dpt._context(conn, tenant)
            suffix = ", CAST(:d AS uuid[])" if name in scoped else ""
            call = text(
                f"SELECT count(*) AS n FROM {SCHEMA}.{name}"
                f"(CAST(:v AS halfvec(1024)), :m, :s, :w{suffix})"
            )
            params: dict[str, object] = {
                "v": vector,
                "m": space[0],
                "s": space[1],
                "w": WANTED,
            }
            if name in scoped:
                params["d"] = scope
            baseline = await dpt._locks(conn)
            try:
                rows = int((await conn.execute(call, params)).scalar_one())
                held = await dpt._locks(conn) - baseline
                timings: list[float] = []
                for _ in range(dpt.CALL_REPEATS):
                    started = time.perf_counter()
                    await conn.execute(call, params)
                    timings.append((time.perf_counter() - started) * 1000.0)
                out[name] = {
                    "locks": held,
                    "returned": rows,
                    "wall_ms": dpt._spread(timings),
                    # Per execution and not only summarised: the interesting thing in this
                    # series is one spike, which is Postgres building its candidate generic
                    # plan on the sixth call, and a median smooths it away where it is largest.
                    "wall_series_ms": [round(value, 3) for value in timings],
                }
            except Exception as exc:  # noqa: BLE001 - a refusal here is a result
                out[name] = {"error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"}
            await conn.rollback()
    return out


_EQUIVALENCE = (
    "WITH shipped AS ("
    "  SELECT c.id AS chunk_id, "
    "         (1 - (e.embedding_half <=> CAST(:v AS halfvec(1024)))) AS score "
    f"  FROM {SCHEMA}.emb e "
    f"  JOIN {SCHEMA}.chk c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id "
    "  WHERE e.embedding_model = :m AND e.embedding_version = :s "
    "    AND c.document_id = ANY(CAST(:d AS uuid[])) "
    "  ORDER BY e.embedding_half <=> CAST(:v AS halfvec(1024)) LIMIT :w), "
    "sh AS (SELECT chunk_id, score, "
    "        row_number() OVER (ORDER BY score DESC, chunk_id) AS rank FROM shipped), "
    "cd AS (SELECT chunk_id, score, "
    "        row_number() OVER (ORDER BY score DESC, chunk_id) AS rank FROM ("
    f"        SELECT * FROM {SCHEMA}.{{arm}}(CAST(:v AS halfvec(1024)), :m, :s, :w, "
    "                                       CAST(:d AS uuid[]))) raw) "
    "SELECT (SELECT count(*) FROM sh) AS shipped_rows, "
    "       (SELECT count(*) FROM cd) AS candidate_rows, "
    "       count(*) AS in_both, "
    "       coalesce(max(abs(sh.score - cd.score)), 0) AS max_score_delta, "
    "       count(*) FILTER (WHERE sh.rank <> cd.rank) AS at_a_different_rank "
    "FROM sh JOIN cd ON cd.chunk_id = sh.chunk_id"
)


async def _equivalence(
    app: AsyncEngine,
    tenant: str,
    vector: str,
    space: tuple[str, str],
    scope: list[str],
    labels: str = "",
) -> dict[str, object]:
    """Bar 4: the same chunk ids at the same ranks, and the scores to the bit.

    Against the literal statement `search.dense()` sends when scoped — written out here rather
    than read from a function — because that is the thing being replaced. Compared per chunk
    rather than by taking a maximum over each side: taking a maximum is what hid a score
    difference in an earlier run of `unpruned-plpgsql-pruning.sql`, where two arms with
    different scores had the same top score and looked identical.
    """
    out: dict[str, object] = {}
    async with app.connect() as conn:
        await dpt._context(conn, tenant, labels)
        for arm in ("f_scoped_bare", "f_scoped_or", "f_shipped_scoped"):
            row = (
                await conn.execute(
                    text(_EQUIVALENCE.format(arm=arm)),
                    {
                        "v": vector,
                        "m": space[0],
                        "s": space[1],
                        "w": WANTED,
                        "d": scope,
                    },
                )
            ).one()
            out[arm] = {
                "shipped_rows": int(row.shipped_rows),
                "candidate_rows": int(row.candidate_rows),
                "in_both": int(row.in_both),
                "max_score_delta": float(row.max_score_delta),
                "rows_at_a_different_rank": int(row.at_a_different_rank),
            }
        await conn.rollback()
    return out


async def _in_step(
    app: AsyncEngine,
    tenant: str,
    vector: str,
    space: tuple[str, str],
    everything: list[str],
) -> dict[str, object]:
    """Bar 6: the scoped function, given every document, must be the unscoped function.

    This is the two-functions-in-step risk expressed as a number rather than as an opinion. A
    scope naming all of a tenant's documents restricts nothing, so the two functions are being
    asked the same question by two different routes; a divergence here is a divergence anywhere,
    and it is the shape a drift between the pair would take on the day one of them was edited.
    """
    async with app.connect() as conn:
        await dpt._context(conn, tenant)
        row = (
            await conn.execute(
                text(
                    "WITH u AS (SELECT chunk_id, score, "
                    "     row_number() OVER (ORDER BY score DESC, chunk_id) AS rank "
                    f"   FROM {SCHEMA}.f_local(CAST(:v AS halfvec(1024)), :m, :s, :w)), "
                    "cd AS (SELECT chunk_id, score, "
                    "     row_number() OVER (ORDER BY score DESC, chunk_id) AS rank "
                    f"   FROM {SCHEMA}.f_scoped_bare(CAST(:v AS halfvec(1024)), :m, :s, :w, "
                    "                               CAST(:d AS uuid[]))) "
                    "SELECT (SELECT count(*) FROM u) AS unscoped_rows, "
                    "       (SELECT count(*) FROM cd) AS scoped_rows, "
                    "       count(*) AS in_both, "
                    "       coalesce(max(abs(u.score - cd.score)), 0) AS max_score_delta, "
                    "       count(*) FILTER (WHERE u.rank <> cd.rank) AS at_a_different_rank "
                    "FROM u JOIN cd ON cd.chunk_id = u.chunk_id"
                ),
                {
                    "v": vector,
                    "m": space[0],
                    "s": space[1],
                    "w": WANTED,
                    "d": everything,
                },
            )
        ).one()
        await conn.rollback()
    return {
        "documents_in_scope": len(everything),
        "unscoped_rows": int(row.unscoped_rows),
        "scoped_rows": int(row.scoped_rows),
        "in_both": int(row.in_both),
        "max_score_delta": float(row.max_score_delta),
        "rows_at_a_different_rank": int(row.at_a_different_rank),
    }


async def _isolation(
    app: AsyncEngine,
    vector: str,
    space: tuple[str, str],
    scopes: dict[str, list[str]],
) -> dict[str, object]:
    """Bar 5: try to make the qualifier and the scope decide instead of the policy.

    Every cell asks a session that is tenant *context* for tenant *requested*'s rows with tenant
    *requested*'s own documents as the scope, so off the diagonal the local, the scope and the
    policy all disagree by construction. Counted against `assign`, which carries no policy, so a
    leak cannot hide inside the arm being tested.

    The plan cache is the second half, and it is the half a single-shot probe cannot see: nine
    calls on one backend under one tenant, then another tenant on the same backend, then the same
    again with a generic plan forced. A fix that isolates correctly until the sixth execution is
    not a fix.
    """
    tenants = [dpt._tenant(index) for index in range(1, TENANTS + 1)]
    grid: list[dict[str, object]] = []
    on_diagonal = 0
    off_diagonal = 0

    async with app.connect() as conn:
        for context in tenants:
            await dpt._context(conn, context)
            for requested in tenants:
                row = (
                    await conn.execute(
                        text(
                            "SELECT count(*) AS n, "
                            "  count(*) FILTER (WHERE a.tenant_id <> CAST(:ctx AS uuid)) AS foreign_rows "
                            f"FROM {SCHEMA}.f_forced_scoped(CAST(:r AS uuid), "
                            "       CAST(:v AS halfvec(1024)), :m, :s, :w, CAST(:d AS uuid[])) f "
                            f"JOIN {SCHEMA}.assign a ON a.chunk_id = f.chunk_id"
                        ),
                        {
                            "ctx": context,
                            "r": requested,
                            "v": vector,
                            "m": space[0],
                            "s": space[1],
                            "w": WANTED,
                            "d": scopes[requested],
                        },
                    )
                ).one()
                grid.append(
                    {
                        "context": context[-2:],
                        "requested": requested[-2:],
                        "rows": int(row.n),
                        "foreign_rows": int(row.foreign_rows),
                    }
                )
                if context == requested:
                    on_diagonal += int(row.n)
                else:
                    off_diagonal += int(row.n)
        await conn.rollback()

    # A document scope is not an access grant: the session's own tenant, asked for its own
    # tenant, with somebody else's documents named in the scope.
    #
    # **Counted as foreign rows and not as rows**, and the first version of this probe got that
    # wrong. `assign` scatters the real corpus over the synthetic tenants by `md5(chunk_id)`, so
    # one real document's passages land in all eight of them and every tenant's "own documents"
    # are largely the same document ids. A scope built from tenant 5's documents therefore
    # selects plenty of tenant 3's rows, and the first reading recorded that as a leak. It is
    # an artefact of how this corpus is synthesised, measured below as
    # `documents_shared_between_tenants` so it cannot be forgotten again. What the policy has to
    # guarantee, and what is asserted, is that none of the rows returned belong to tenant 5.
    async with app.connect() as conn:
        await dpt._context(conn, dpt._tenant(3))
        row = (
            await conn.execute(
                text(
                    "SELECT count(*) AS n, "
                    "  count(*) FILTER (WHERE a.tenant_id <> CAST(:ctx AS uuid)) AS foreign_rows "
                    f"FROM {SCHEMA}.f_scoped_bare(CAST(:v AS halfvec(1024)), :m, :s, :w, "
                    "                             CAST(:d AS uuid[])) f "
                    f"JOIN {SCHEMA}.assign a ON a.chunk_id = f.chunk_id"
                ),
                {
                    "ctx": dpt._tenant(3),
                    "v": vector,
                    "m": space[0],
                    "s": space[1],
                    "w": WANTED,
                    "d": scopes[dpt._tenant(5)],
                },
            )
        ).one()
        foreign_scope = {"rows": int(row.n), "foreign_rows": int(row.foreign_rows)}
        await conn.rollback()

    # The plan cache: nine calls as tenant 3, then tenant 5 on the same backend, custom and
    # then forced generic.
    cache: list[dict[str, object]] = []
    for forced in (False, True):
        async with app.connect() as conn:
            await dpt._context(conn, dpt._tenant(3))
            if forced:
                await conn.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
            call = text(
                "SELECT count(*) AS n, "
                "  count(*) FILTER (WHERE a.tenant_id <> CAST(:ctx AS uuid)) AS foreign_rows "
                f"FROM {SCHEMA}.f_scoped_bare(CAST(:v AS halfvec(1024)), :m, :s, :w, "
                "                             CAST(:d AS uuid[])) f "
                f"JOIN {SCHEMA}.assign a ON a.chunk_id = f.chunk_id"
            )
            base = {"v": vector, "m": space[0], "s": space[1], "w": WANTED}
            for _ in range(dpt.CALL_REPEATS):
                await conn.execute(
                    call, {**base, "ctx": dpt._tenant(3), "d": scopes[dpt._tenant(3)]}
                )
            await dpt._context(conn, dpt._tenant(5))
            row = (
                await conn.execute(
                    call, {**base, "ctx": dpt._tenant(5), "d": scopes[dpt._tenant(5)]}
                )
            ).one()
            cache.append(
                {
                    "after_nine_calls_as_tenant_3": "tenant 5, forced generic"
                    if forced
                    else "tenant 5, custom",
                    "rows": int(row.n),
                    "foreign_rows": int(row.foreign_rows),
                }
            )
            await conn.rollback()

    return {
        "cells": len(grid),
        "rows_on_the_diagonal": on_diagonal,
        "rows_off_the_diagonal": off_diagonal,
        "foreign_rows": sum(int(cell["foreign_rows"]) for cell in grid),
        "another_tenants_documents_in_scope": foreign_scope,
        "plan_cache": cache,
        "grid": grid,
    }


#: Executions in the promotion watch. Postgres builds a candidate generic plan once a prepared
#: statement has been planned custom five times, and `dense-plan-time.json` read the resulting
#: spike as the statement *reaching* a generic plan unforced. Building one and using one are
#: different things — the plancache costs the candidate and keeps planning custom if the custom
#: plans are cheaper — and that difference is the whole weight of the argument against the
#: scoped shape. Forty executions, so the claim is measured rather than argued from eleven.
PROMOTION_WATCH = 40


async def _promotion(
    app: AsyncEngine,
    tenant: str,
    vector: str,
    space: tuple[str, str],
    scope: list[str],
) -> dict[str, object]:
    """Is the generic plan ever reached without being forced?

    The premise this file inherits says the forced-generic plan is what a pooled connection
    reaches in the course of ordinary traffic. If that is right, the plan changes shape
    somewhere after the fifth execution and the HNSW index disappears from it on its own. If it
    is wrong — if Postgres builds the candidate, prices it and goes on planning custom — then
    the forced arms are a bound on the damage rather than a forecast of it, and the two are very
    different things to ship on.

    Nothing is forced here and nothing is reset between executions: one backend, one prepared
    statement, `PROMOTION_WATCH` executions, and the plan read every time.
    """
    vec, mdl, ver = dpt._literal(vector), dpt._literal(space[0]), dpt._literal(space[1])
    tid, docs = dpt._literal(tenant), _array(scope)
    calls = {
        "today": f"EXECUTE p_today({vec}, {mdl}, {ver}, {WANTED})",
        "local": f"EXECUTE p_local({tid}, {vec}, {mdl}, {ver}, {WANTED})",
        "shipped_scoped": f"EXECUTE p_shipped_scoped({vec}, {mdl}, {ver}, {WANTED}, {docs})",
        "scoped_bare": f"EXECUTE p_scoped_bare({tid}, {vec}, {mdl}, {ver}, {WANTED}, {docs})",
    }
    prepares = [
        statement.format(schema=SCHEMA).strip()
        for statement in list(dpt._PREPARES.values()) + list(_PREPARES.values())
    ]

    out: dict[str, object] = {}
    for name, call in calls.items():
        async with app.connect() as conn:
            await dpt._context(conn, tenant)
            for statement in prepares:
                await conn.execute(text(statement))
            hnsw: list[bool] = []
            planning: list[float] = []
            for _ in range(PROMOTION_WATCH):
                lines = await dpt._explain(conn, call)
                hnsw.append(bool(_flags(lines)["hnsw_index_in_plan"]))
                value = dpt._read_plan(lines)["planning_ms"]
                if value is not None:
                    planning.append(round(float(value), 3))
            out[name] = {
                "executions": PROMOTION_WATCH,
                "hnsw_index_every_execution": all(hnsw),
                "hnsw_lost_at_execution": (hnsw.index(False) + 1) if False in hnsw else None,
                "planning_series_ms": planning,
            }
            await conn.rollback()
    return out


async def _environment(owner: AsyncEngine) -> dict[str, object]:
    """The installation these readings were taken against, written into the report.

    Not decoration. This branch's measurements straddled a deploy — the shared installation went
    from migration 0025 to 0026, `chunks` and `chunk_embeddings` became partitioned in `public`,
    and `max_locks_per_transaction` went from 64 to 2,560 when the container was recreated,
    taking the cluster-wide lock table from 6,400 slots to 256,000. A lock count from one side
    of that is not comparable with a lock count from the other, and a report that does not say
    which side it came from cannot be placed by whoever reads it next.
    """
    async with owner.connect() as conn:
        settings_rows = list(
            await conn.execute(
                text(
                    "SELECT name, setting FROM pg_settings WHERE name IN "
                    "('max_locks_per_transaction', 'max_connections', 'server_version', "
                    " 'plan_cache_mode', 'shared_buffers', 'work_mem')"
                )
            )
        )
        head = str(
            (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        )
        partitions = int(
            (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_inherits i JOIN pg_class p "
                        "ON p.oid = i.inhparent WHERE p.relname = 'chunks'"
                    )
                )
            ).scalar_one()
        )
        shared = int(
            (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM (SELECT document_id FROM "
                        f"{SCHEMA}.chk GROUP BY document_id "
                        "HAVING count(DISTINCT tenant_id) > 1) x"
                    )
                )
            ).scalar_one()
        )
        await conn.rollback()
    values = {str(row.name): str(row.setting) for row in settings_rows}
    return {
        **values,
        "lock_slots": int(values["max_locks_per_transaction"]) * int(values["max_connections"]),
        "alembic_head": head,
        "public_chunks_partitions": partitions,
        # The synthetic tenants share document ids, because `assign` scatters one real
        # document's passages across all eight of them. Recorded so the isolation section's
        # "another tenant's documents" probe is read as the weak test it is.
        "documents_shared_between_tenants": shared,
    }


# --- One rung ------------------------------------------------------------------------------


async def _rung(
    owner: AsyncEngine, app: AsyncEngine, modulus: int, space: tuple[str, str]
) -> dict[str, object]:
    built = await dpt._build(owner, modulus, space)
    await _functions(owner)
    tenant = dpt._tenant(3)
    vector, model, version = await dpt._probe_vector(owner, tenant)
    space = (model, version)
    scopes = await _scopes(owner)
    scope = scopes[tenant]
    everything = await _all_documents(owner, tenant)
    async with owner.connect() as conn:
        in_scope = int(
            (
                await conn.execute(
                    text(
                        f"SELECT count(*) FROM {SCHEMA}.chk WHERE tenant_id = CAST(:t AS uuid) "
                        "AND document_id = ANY(CAST(:d AS uuid[]))"
                    ),
                    {"t": tenant, "d": scope},
                )
            ).scalar_one()
        )
        on_tenant = int(
            (
                await conn.execute(
                    text(f"SELECT count(*) FROM {SCHEMA}.chk WHERE tenant_id = CAST(:t AS uuid)"),
                    {"t": tenant},
                )
            ).scalar_one()
        )
        await conn.rollback()
    return {
        "modulus": modulus,
        "built_seconds": built,
        "environment": await _environment(owner),
        "scope": {
            "documents": len(scope),
            "chunks_in_scope": in_scope,
            "chunks_on_the_tenant": on_tenant,
            "documents_on_the_tenant": len(everything),
        },
        "plans": await _plans(app, tenant, vector, space, scope),
        "promotion": await _promotion(app, tenant, vector, space, scope),
        "calls": await _locks(app, tenant, vector, space, scope),
        "equivalence": await _equivalence(app, tenant, vector, space, scope),
        "equivalence_under_one_label": await _equivalence(
            app, tenant, vector, space, scope, dpt.LABEL_A
        ),
        "in_step": await _in_step(app, tenant, vector, space, everything),
        "isolation": await _isolation(app, vector, space, scopes),
    }


async def _teardown(owner: AsyncEngine) -> None:
    async with owner.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        for signature in (
            "f_shipped_scoped(halfvec(1024), text, text, int, uuid[])",
            "f_scoped_or(halfvec(1024), text, text, int, uuid[])",
            "f_scoped_bare(halfvec(1024), text, text, int, uuid[])",
            "f_forced_scoped(uuid, halfvec(1024), text, text, int, uuid[])",
        ):
            with contextlib.suppress(Exception):
                await conn.execute(text(f"DROP FUNCTION IF EXISTS {SCHEMA}.{signature} CASCADE"))
    await dpt._teardown(owner)


# --- The verdict ---------------------------------------------------------------------------


def _zero(value: object) -> float:
    """A number read out of the report, with a missing one failing rather than passing.

    `float(x or 0)` reads an absent reading as a pass and `float(x or 1)` reads a passing zero
    as a failure. Both were in the first version of `_bars` and both were wrong in the direction
    that hides the answer.
    """
    return -1.0 if value is None else float(value)  # pyright: ignore[reportArgumentType]


def _bars(rungs: list[dict[str, Any]]) -> dict[str, object]:
    """The seven bars, read off the written report rather than evaluated at measurement time.

    Read off what was stored so a bar cannot be quietly relaxed by the code that also decides
    whether it passed.
    """
    failures: list[str] = []
    measured = [rung for rung in rungs if "plans" in rung]

    # A rung that raised is not a rung that passed. Without this line every bar below reads
    # `True` over an empty list and the report announces seven passes for a run that never
    # reached the database — which is the shape of three null results this week.
    for rung in rungs:
        if "plans" not in rung:
            for bar in range(1, 8):
                failures.append(
                    f"modulus {rung.get('modulus')}: Bar {bar} unmeasured, the rung failed -- "
                    f"{rung.get('error')}"
                )

    for rung in measured:
        modulus = int(rung["modulus"])
        plans: dict[str, Any] = rung["plans"]

        def arm(name: str) -> dict[str, Any]:
            return plans.get(name) or {}

        # Bar 1 — the controls reproduce.
        if not arm("local_forced_generic").get("hnsw_index_in_plan"):
            failures.append(
                f"modulus {modulus}: Bar 1, the unscoped candidate lost the HNSW index under a "
                "forced generic plan, which is not what 5f-8 recorded -- this run measured "
                "something else"
            )
        if arm("scoped_or_forced_generic").get("hnsw_index_in_plan"):
            failures.append(
                f"modulus {modulus}: Bar 1, the NULL-guard arm kept the HNSW index under a "
                "forced generic plan, which is not what 5f-6 recorded -- this run measured "
                "something else"
            )

        # Bar 2 — the deciding question.
        if not arm("scoped_bare_forced_generic").get("hnsw_index_in_plan"):
            failures.append(
                f"modulus {modulus}: Bar 2, scoped_bare lost the HNSW index under a forced "
                f"generic plan -- seq scans {arm('scoped_bare_forced_generic').get('seq_scans')}"
            )

        # Bar 3 — plan-time pruning under a custom plan.
        bare = arm("scoped_bare")
        if int(bare.get("partitions_in_plan") or 0) != PARTITIONS_WHEN_PRUNED:
            failures.append(
                f"modulus {modulus}: Bar 3, scoped_bare named "
                f"{bare.get('partitions_in_plan')} partitions, not {PARTITIONS_WHEN_PRUNED}"
            )
        if bare.get("subplans_removed"):
            failures.append(
                f"modulus {modulus}: Bar 3, scoped_bare reported Subplans Removed "
                f"{bare.get('subplans_removed')} -- that is executor-startup pruning, not "
                "plan-time"
            )
        if not bare.get("policy_became_a_one_time_filter"):
            failures.append(
                f"modulus {modulus}: Bar 3, scoped_bare's policy did not fold into a "
                "One-Time Filter"
            )
        if not bare.get("hnsw_index_in_plan"):
            failures.append(
                f"modulus {modulus}: Bar 3, scoped_bare did not name the HNSW index under a "
                "custom plan"
            )

        # Bar 4 — the rows, against the statement search.dense() sends today.
        #
        # `_zero` rather than `x or 0`, and the difference is not style. `0.0 or 1` is `1`, so
        # the obvious idiom reads a passing zero as a failing one and every one of Bar 4, 5 and
        # 6 failed on its first run with the numbers printed beside it saying they had passed.
        # That is the same class of mistake as a bar that cannot fail, reached from the other
        # side.
        for section in ("equivalence", "equivalence_under_one_label"):
            same: dict[str, Any] = (rung.get(section) or {}).get("f_scoped_bare") or {}
            if (
                same.get("shipped_rows") != same.get("candidate_rows")
                or same.get("in_both") != same.get("shipped_rows")
                or _zero(same.get("max_score_delta")) != 0.0
                or _zero(same.get("rows_at_a_different_rank")) != 0
                or _zero(same.get("candidate_rows")) <= 0
            ):
                failures.append(f"modulus {modulus}: Bar 4 ({section}) {same}")

        # Bar 5 — the qualifier and the scope must never decide.
        isolation: dict[str, Any] = rung.get("isolation") or {}
        if _zero(isolation.get("rows_off_the_diagonal")) != 0:
            failures.append(
                f"modulus {modulus}: Bar 5, {isolation.get('rows_off_the_diagonal')} rows off "
                "the diagonal"
            )
        if _zero(isolation.get("foreign_rows")) != 0:
            failures.append(
                f"modulus {modulus}: Bar 5, {isolation.get('foreign_rows')} foreign rows"
            )
        if _zero(isolation.get("rows_on_the_diagonal")) <= 0:
            failures.append(f"modulus {modulus}: Bar 5, nothing on the diagonal either")
        scoped_probe: dict[str, Any] = isolation.get("another_tenants_documents_in_scope") or {}
        if _zero(scoped_probe.get("foreign_rows")) != 0:
            failures.append(
                f"modulus {modulus}: Bar 5, a scope naming another tenant's documents returned "
                f"{scoped_probe.get('foreign_rows')} of that tenant's rows"
            )
        for entry in isolation.get("plan_cache") or []:
            if _zero(entry.get("foreign_rows")) != 0 or _zero(entry.get("rows")) <= 0:
                failures.append(f"modulus {modulus}: Bar 5, plan cache {entry}")

        # Bar 6 — the pair cannot drift apart silently.
        step: dict[str, Any] = rung.get("in_step") or {}
        if (
            step.get("unscoped_rows") != step.get("scoped_rows")
            or step.get("in_both") != step.get("unscoped_rows")
            or _zero(step.get("max_score_delta")) != 0.0
            or _zero(step.get("rows_at_a_different_rank")) != 0
            or _zero(step.get("scoped_rows")) <= 0
        ):
            failures.append(f"modulus {modulus}: Bar 6 {step}")

        # Bar 7 — locks.
        held = ((rung.get("calls") or {}).get("f_scoped_bare") or {}).get("locks")
        if held is None or int(held) > LOCK_CEILING:
            failures.append(f"modulus {modulus}: Bar 7, scoped_bare held {held} locks")

    def passed(bar: str) -> bool:
        # `and measured` is the whole point: a bar evaluated over an empty list is not a bar
        # that passed, and a report announcing seven passes for a run that never reached the
        # database is exactly the failure this project has produced four times this week.
        return bool(measured) and not any(bar in entry for entry in failures)

    return {
        "measured_rungs": len(measured),
        "bar_1_controls_reproduced": passed("Bar 1"),
        "bar_2_hnsw_survives_a_generic_plan": passed("Bar 2"),
        "bar_3_prunes_at_plan_time": passed("Bar 3"),
        "bar_4_identical_rows": passed("Bar 4"),
        "bar_5_isolation": passed("Bar 5"),
        "bar_6_the_pair_agrees": passed("Bar 6"),
        "bar_7_locks": passed("Bar 7"),
        "all_seven": bool(measured) and not failures,
        "failures": failures,
    }


def _baseline(rungs: list[dict[str, Any]]) -> list[dict[str, object]]:
    """What production does today under the same forced generic plan, judged by nobody.

    The one reading that turns "scoped_bare lost the index" into a regression or a wash.
    """
    out: list[dict[str, object]] = []
    for rung in rungs:
        plans: dict[str, Any] = rung.get("plans") or {}
        row: dict[str, object] = {"modulus": rung.get("modulus")}
        for name in (
            "today",
            "local",
            "shipped_scoped",
            "scoped_or",
            "scoped_bare",
            "today_forced_generic",
            "local_forced_generic",
            "shipped_scoped_forced_generic",
            "scoped_or_forced_generic",
            "scoped_bare_forced_generic",
        ):
            arm: dict[str, Any] = plans.get(name) or {}
            row[name] = {
                "hnsw": arm.get("hnsw_index_in_plan"),
                "partitions": arm.get("partitions_in_plan"),
                "removed": arm.get("subplans_removed"),
                "planning_ms": (arm.get("planning_ms") or {}).get("median_warm"),
                "execution_ms": (arm.get("execution_ms") or {}).get("median_warm"),
                "seq_scans": arm.get("seq_scans"),
            }
        row["locks"] = {
            name: (rung.get("calls") or {}).get(name, {}).get("locks")
            for name in ("f_shipped", "f_local", "f_shipped_scoped", "f_scoped_bare")
        }
        out.append(row)
    return out


def _verdict(rungs: list[dict[str, Any]]) -> list[dict[str, object]]:
    """The scoped pair against the scoped baseline, and the unscoped pair against theirs.

    Written as a comparison rather than as a score, because "lost the HNSW index" is only a
    regression if the statement being replaced kept it. Bar 2 is judged in absolute terms on
    purpose — a shape that reads a whole partition sequentially is a bad shape at production
    partition sizes whatever the shape beside it does — and this table is what says whether
    failing it costs anything against today.
    """
    out: list[dict[str, object]] = []
    for rung in rungs:
        plans: dict[str, Any] = rung.get("plans") or {}
        promotion: dict[str, Any] = rung.get("promotion") or {}

        def hnsw(name: str) -> object:
            return (plans.get(name) or {}).get("hnsw_index_in_plan")

        out.append(
            {
                "modulus": rung.get("modulus"),
                "unscoped_generic_baseline_keeps_hnsw": hnsw("today_forced_generic"),
                "unscoped_candidate_generic_keeps_hnsw": hnsw("local_forced_generic"),
                "scoped_generic_baseline_keeps_hnsw": hnsw("shipped_scoped_forced_generic"),
                "scoped_bare_generic_keeps_hnsw": hnsw("scoped_bare_forced_generic"),
                "scoped_or_generic_keeps_hnsw": hnsw("scoped_or_forced_generic"),
                "generic_plan_ever_reached_unforced": {
                    name: entry.get("hnsw_lost_at_execution")
                    for name, entry in promotion.items()
                },
            }
        )
    return out


async def _run(rungs: list[int]) -> int:
    owner, app = dpt._engines()
    report: dict[str, Any] = {
        "question": (
            "If the disjunct is what costs the HNSW index under a generic plan, does splitting "
            "the dense candidate into an unscoped function and a never-NULL scoped one get the "
            "index back -- or does `= ANY($6)` cost it anyway?"
        ),
        "premise": "eval/dense-plan-time-pruning.sql section 5f, and eval/dense-plan-time.json",
        "schema": SCHEMA,
        "wanted": WANTED,
        "tenants": TENANTS,
        "scope_documents": SCOPE_DOCUMENTS,
        "plan_repeats": PLAN_REPEATS,
        "lock_ceiling": LOCK_CEILING,
        "rungs": [],
    }
    try:
        space = await dpt._space(owner)
        report["embedding_space"] = {"model": space[0], "version": space[1]}
        await dpt._drop(owner)
        report["assign"] = await dpt._assign(owner, space)
        for modulus in rungs:
            print(f"--- modulus {modulus}", flush=True)
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
                owner, app = dpt._engines()
    finally:
        with contextlib.suppress(Exception):
            await dpt._drop(owner)
        report["verified"] = await dpt._verify(owner)
        await owner.dispose()
        await app.dispose()

    report["baseline"] = _baseline(report["rungs"])
    report["verdict"] = _verdict(report["rungs"])
    report["bars"] = _bars(report["rungs"])
    REPORT.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n")

    print(f"\nWrote {REPORT}")
    for row in report["baseline"]:
        print(f"  modulus {row['modulus']}")
        for name, arm in row.items():
            if name in ("modulus", "locks") or not isinstance(arm, dict):
                continue
            print(
                f"    {name:<32} hnsw {str(arm['hnsw']):<5} "
                f"partitions {str(arm['partitions']):>4}  removed {arm['removed']}  "
                f"plan {arm['planning_ms']} ms  exec {arm['execution_ms']} ms"
            )
        print(f"    locks {row.get('locks')}")
    for row in report["verdict"]:
        print(f"  verdict at modulus {row['modulus']}: {json.dumps(row)}")
    bars: dict[str, Any] = report["bars"]
    for key, value in bars.items():
        if key != "failures":
            print(f"  {key}: {value}")
    for failure in bars["failures"]:
        print(f"  ! {failure}")
    return 0 if bars["all_seven"] else 1


COMMAND = "dense-two-functions"
USAGE = "dense-two-functions [--partitions 32,256]"


def cli(argv: list[str]) -> int:
    rungs = list(RUNGS)
    for index, argument in enumerate(argv):
        if argument == "--partitions" and index + 1 < len(argv):
            rungs = [int(part) for part in argv[index + 1].split(",")]
    return asyncio.run(_run(rungs))


def run() -> int:
    return cli(sys.argv[1:])
