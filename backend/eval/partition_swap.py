# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""What migration 0026 actually did to this installation.

`partition-shape.json` measured partitioning on scratch tables: a copy of the corpus, one
index per partition, a query written for the sweep. This measures the thing itself — the
real `chunks` and `chunk_embeddings`, the real indexes, the SQL `search.dense()` builds,
read as `zenith_app` under a real tenant's context, with no `WHERE` clause the product does
not issue.

**The bars below are written before the run and a failed one stays in the file.** A
measurement whose threshold is decided after the numbers are in is not a measurement. Each
one is recorded with the value that met or missed it, so a later reader can see which.

**A perfect result usually means the measurement did not happen.** So the plan is recorded,
not only the verdict: `pruning.plans` holds the `EXPLAIN (ANALYZE)` text for each arm, and
`Subplans Removed` is read out of it rather than asserted. A null result and an experiment
that never ran look identical unless the plan is on disk.

Read-only. Every statement is a `SELECT` or an `EXPLAIN`, the connection is `READ ONLY`, and
the transaction is rolled back.

    ZENITH_DATABASE_OWNER_URL=... python -m eval partition-swap
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine

from app.core.config import settings
from app.features.embeddings.client import DIMENSION, MODEL, VERSION
from app.features.retrieval.search import CANDIDATES, dense
from eval.harness import installation, score

COMMAND = "partition-swap"
USAGE = "partition-swap"

REPORT = Path(__file__).parent / "partition-swap.json"

#: The corpus this installation holds, counted before the migration and required back
#: afterwards. Not a round number and not a target: it is what was there.
CORPUS_BAR = {"documents": 42, "chunks": 13549, "embeddings": 13549}

#: `live-recall.json`'s headline, which migration 0025 also had to hold. Recall is allowed to
#: be higher and is not allowed to be lower: partitioning changes which rows a scan visits
#: and must not change which rows come back.
RECALL_BAR = {"headline_recall_at_8": 0.9, "recall_at_1_all": 0.6667}

#: At most one partition of each partitioned relation may appear in an arm's plan.
#:
#: **This replaces a bar that said `Subplans Removed >= 255`, and the replacement is
#: stronger rather than weaker.** That bar assumed runtime pruning was the only kind. It is
#: not: pruning on a *parameter* happens at plan time, and a plan that never built the other
#: 255 subplans has nothing to remove and prints no `Subplans Removed` line at all. So the
#: better outcome scored zero against the old bar — the lexical arm went from
#: `Subplans Removed: 255` with 1,543 locks to no line at all with 13, and the first reading
#: was "pruning stopped working".
#:
#: Counting partitions in the plan covers both mechanisms and cannot be satisfied by either
#: failure: a plan that scans 256 partitions fails it whether or not it removes them later.
#: `subplans_removed` and `planning_ms` are still recorded beside it, because *which* of the
#: two mechanisms ran is the difference between 13 locks and 1,543 and no single number says
#: it.
PARTITIONS_IN_PLAN_BAR = 1

#: Superseded by the above and kept because a bar that failed does not get deleted. It was
#: `Subplans Removed >= 255` per `Append`, and the dense arm still meets it — its predicate
#: comes from the RLS policy, which is `STABLE` by design, so runtime pruning is the only
#: mechanism available to it and 255 is the right number. The lexical arm no longer meets it
#: and is better for not doing so.
SUPERSEDED_SUBPLANS_BAR = 255

#: The modulus 0026 ships. Read back from the catalogue rather than trusted, because a
#: partial repartition is the failure that reads as success.
MODULUS_BAR = 256

#: The lexical arm must prune at *plan* time, and this is the only number that says so.
#:
#: `Subplans Removed` says the executor skipped 255 partitions; it says nothing about whether
#: the planner built and locked paths for them first. The redundant-qualifier version of
#: `zenith_lexical_search` scored a perfect `Subplans Removed: 255` and held 1,543 locks, and
#: the two readings are indistinguishable without this one. A bar well under a hundred fails
#: the moment plan-time pruning stops — a generic plan, a lost plpgsql local, a predicate
#: rewritten into something the planner cannot fold.
#:
#: There is deliberately no bar on the dense arm. Its tenant predicate comes from the RLS
#: policy, which is `zenith_current_tenant()` and is `STABLE` by design, so it prunes at
#: runtime and locks all 256 — about 2,341 relations. Making that prune at plan time would
#: mean the application binding a tenant into the query, which is invariant 1. The cost is
#: recorded rather than fixed, because it is the price of the design and not a defect in it.
LEXICAL_LOCK_BAR = 100

#: Rows each arm must actually return. `CANDIDATES` is what both halves ask for and what
#: this corpus can supply; anything less and the plan beside it is describing a query that
#: found nothing, whose `Subplans Removed` is perfect and meaningless.
ROWS_BAR = CANDIDATES


async def _dense_statements() -> list[str]:
    """Everything `search.dense()` issues, in order, taken from the function not retyped.

    The same recorder trick `test_vector_index.py` uses, and for the same reason: a copy of
    the product's SQL in a measurement is a copy that drifts, and a drifted copy here would
    publish a plan for a query nothing runs.

    **All of them, and the first draft of this file took only the last.** `dense()` issues
    two `SET LOCAL`s before its `SELECT` — `hnsw.ef_search` from the hardware profile and
    `hnsw.iterative_scan = relaxed_order` — and the second is not a tuning knob: without it
    the HNSW scan takes its `ef_search` nearest neighbours from a graph shared by every
    tenant and the policy discards them afterwards. Planning only the `SELECT` measured that
    exact defect and returned `actual rows=0`, which is a plan for a query the product does
    not run and would have been recorded as a pruning result.
    """

    class Recorder:
        def __init__(self) -> None:
            self.statements: list[str] = []

        async def execute(self, clause: object, params: object = None) -> list[Any]:
            self.statements.append(str(clause))
            return []

    recorder = Recorder()
    await dense(
        cast(AsyncSession, recorder),
        embedding=[0.0] * DIMENSION,
        model=MODEL,
        version=VERSION,
        ef_search=100,
    )
    return recorder.statements


#: The body of `zenith_lexical_search`, read out of the catalogue. The function is
#: `SECURITY DEFINER`, so `EXPLAIN` of a call to it shows a function scan and nothing about
#: what it does inside; the only way to see whether the lexical half prunes is to plan the
#: statement the function runs. Read rather than retyped, so it cannot describe a function
#: that is no longer installed.
_FUNCTION_BODY = "SELECT prosrc FROM pg_proc WHERE proname = 'zenith_lexical_search'"


def _lexical_arm(source: str) -> str:
    """The `RETURN QUERY` statement out of the installed function, with its two arguments
    bound.

    Cut from `prosrc` rather than written here, and the first draft of this file did write it
    here. It then reported `Subplans Removed: []` for an installation whose function had just
    been rewritten to prune — a measurement of a query nobody runs, published beside one that
    did run, in the same report. The same argument as `_dense_statements`: a copy of the
    product's SQL inside a measurement is a copy that drifts.
    """
    body = source.split("RETURN QUERY", 1)[1]
    statement = body.split("LIMIT want;", 1)[0] + "LIMIT :want"
    # `v_tenant` is a plpgsql local, and a plpgsql local *is* a parameter of the statement
    # underneath — that is the whole mechanism this migration relies on for plan-time
    # pruning. Binding it here is not an approximation of what plpgsql does; it is the same
    # thing, and it is the only way to plan the body from outside the function, because
    # `EXPLAIN` on a call reports `Function Scan` and one number.
    return statement.replace("query_string", ":question").replace("v_tenant", ":tenant")


#: Case-insensitive, because Postgres deparses the bound as `FOR VALUES WITH (modulus 256,
#: remainder 7)`. An uppercase pattern here matched nothing and reported `modulus: null`
#: beside `partitions: 256` — the same defect, and the same fix, as `_HASH_BOUNDS` in
#: `tests/integration/test_partition_rls_guard.py`.
_SHAPE = """
SELECT parent.relname,
       count(*) AS partitions,
       min((regexp_match(pg_get_expr(child.relpartbound, child.oid),
                         'modulus (\\d+)', 'i'))[1]::int) AS modulus,
       max((regexp_match(pg_get_expr(child.relpartbound, child.oid),
                         'modulus (\\d+)', 'i'))[1]::int) AS max_modulus
FROM pg_class child
JOIN pg_class parent ON parent.oid = pg_partition_root(child.oid)
JOIN pg_namespace n ON n.oid = child.relnamespace
WHERE n.nspname = 'public' AND child.relispartition
  AND parent.relname IN ('chunks', 'chunk_embeddings')
GROUP BY parent.relname ORDER BY parent.relname
"""

#: Relations a query has to lock: every partition and every index on it. This is the
#: multiplier `partition-shape.json` could only estimate, because its scratch tables carried
#: one index per partition and the production ones carry more.
_RELATIONS = """
SELECT parent.relname, count(*) + count(*) FILTER (WHERE ix.n IS NOT NULL) * 0 + sum(
           coalesce(ix.n, 0)) AS relations
FROM pg_class child
JOIN pg_class parent ON parent.oid = pg_partition_root(child.oid)
JOIN pg_namespace n ON n.oid = child.relnamespace
LEFT JOIN LATERAL (
    SELECT count(*) AS n FROM pg_index i WHERE i.indrelid = child.oid
) ix ON true
WHERE n.nspname = 'public' AND child.relispartition
  AND parent.relname IN ('chunks', 'chunk_embeddings')
GROUP BY parent.relname ORDER BY parent.relname
"""

_CORPUS = """
SELECT (SELECT count(*) FROM documents) AS documents,
       (SELECT count(*) FROM chunks) AS chunks,
       (SELECT count(*) FROM chunk_embeddings) AS embeddings,
       (SELECT count(*) FROM query_citations) AS citations,
       (SELECT count(DISTINCT tenant_id) FROM chunks) AS tenants_with_chunks
"""


_PLANNING = re.compile(r"Planning Time: ([\d.]+) ms")


def _subplans_removed(plan: str) -> list[int]:
    return [int(match) for match in re.findall(r"Subplans Removed: (\d+)", plan)]


_PARTITION = re.compile(r"\b(chunks|chunk_embeddings)_p(\d{3})\b")


def _partitions_in_plan(plan: str) -> dict[str, int]:
    """How many distinct partitions of each parent the plan names.

    Distinct, because a partition appears once per node that touches it — the dense arm
    names its `chunks` partition twice, as a relation and as the index on it.
    """
    found: dict[str, set[str]] = {}
    for parent, remainder in _PARTITION.findall(plan):
        found.setdefault(parent, set()).add(remainder)
    return {parent: len(remainders) for parent, remainders in found.items()}


async def _as_app(connection: AsyncConnection, tenant: str, labels: list[str]) -> None:
    """The role and the context the product runs with, and nothing else.

    `zenith_app` rather than the owner, because the owner bypasses every policy and it is
    the policy that carries the tenant equality the planner prunes on. Measuring as the owner
    would be measuring a query the product never issues.
    """
    await connection.execute(text("SET LOCAL ROLE zenith_app"))
    await connection.execute(text("SELECT set_config('zenith.tenant_id', :t, true)"), {"t": tenant})
    await connection.execute(
        text("SELECT set_config('zenith.label_ids', :l, true)"), {"l": ",".join(labels)}
    )


async def _plan_and_locks(
    connection: AsyncConnection, sql: str, params: dict[str, object]
) -> dict[str, object]:
    plan = "\n".join(
        str(row[0])
        for row in await connection.execute(
            text("EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY ON) " + sql), params
        )
    )
    locks = await connection.scalar(
        text("SELECT count(*) FROM pg_locks WHERE pid = pg_backend_pid() AND locktype = 'relation'")
    )
    return {
        # Which partitions the plan actually names, per partitioned parent. One each is the
        # bar; 256 is the unpruned failure, whether or not it is removed afterwards.
        "partitions_in_plan": _partitions_in_plan(plan),
        "subplans_removed": _subplans_removed(plan),
        # Planning time is the cost partitioning *adds*, and `SUMMARY ON` is the only place
        # it is visible. `chore/partition-lock-budget` reports 55-75 ms at this modulus and
        # says the prepared-statement path does not amortise it; this records what the same
        # statements cost here so the two can be compared rather than believed.
        "planning_ms": float(_PLANNING.search(plan).group(1))  # type: ignore[union-attr]
        if _PLANNING.search(plan)
        else None,
        # Locks are taken at *planning* time, on every partition, whatever pruning removes
        # afterwards. This is the number that decides concurrency, and it does not move when
        # `Subplans Removed` appears — which is exactly the trap the first fix to
        # `zenith_lexical_search` fell into.
        "locks_held_after": int(locks or 0),
        # The outermost node's row count. An arm that returned nothing prunes perfectly and
        # measures nothing, and the two are indistinguishable in a `Subplans Removed` line —
        # so the count is recorded and there is a bar on it below.
        "rows": int(re.search(r"actual rows=(\d+)", plan).group(1))  # type: ignore[union-attr]
        if re.search(r"actual rows=(\d+)", plan)
        else 0,
        "plan": plan,
    }


async def _measure() -> dict[str, object]:
    engine = create_async_engine(settings.database_owner_url)
    *dense_setup, dense_sql = await _dense_statements()

    async with engine.connect() as connection:
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        corpus = dict((await connection.execute(text(_CORPUS))).mappings().one())
        shape = {
            row.relname: {"partitions": row.partitions, "modulus": row.modulus}
            for row in await connection.execute(text(_SHAPE))
        }
        relations = {
            row.relname: int(row.relations) for row in await connection.execute(text(_RELATIONS))
        }
        settings_rows = {
            row.name: row.setting
            for row in await connection.execute(
                text(
                    "SELECT name, setting FROM pg_settings WHERE name IN "
                    "('max_locks_per_transaction', 'max_connections', 'shared_buffers')"
                )
            )
        }
        tenant = str(
            await connection.scalar(
                text("SELECT tenant_id FROM chunks GROUP BY 1 ORDER BY count(*) DESC LIMIT 1")
            )
        )
        labels = [
            str(value)
            for value in (
                await connection.execute(
                    text("SELECT id FROM access_labels WHERE tenant_id = :t"), {"t": tenant}
                )
            ).scalars()
        ]
        installed_source = await connection.scalar(text(_FUNCTION_BODY))
        assert installed_source, "zenith_lexical_search is not installed"
        lexical_arm = _lexical_arm(str(installed_source))
        # A passage the measured tenant actually holds, rather than a basis vector. A
        # synthetic query vector lands nowhere near any cluster in the graph, which is a
        # different question from the one being asked and — with `ef_search` bounded — can
        # legitimately return nothing. `partition-shape.json` used a stored embedding for
        # the same reason.
        vector = str(
            await connection.scalar(
                text(
                    "SELECT embedding FROM chunk_embeddings WHERE tenant_id = :t "
                    "ORDER BY chunk_id LIMIT 1"
                ),
                {"t": tenant},
            )
        )

        await _as_app(connection, tenant, labels)
        for statement in dense_setup:
            await connection.execute(text(statement))
        arms = {
            "dense": await _plan_and_locks(
                connection,
                dense_sql,
                {
                    "model": MODEL,
                    "version": VERSION,
                    "embedding": vector,
                    "limit": CANDIDATES,
                },
            ),
        }
        await connection.rollback()

    # The lexical arm is planned as the owner, because that is who
    # `zenith_lexical_search` executes as — it is `SECURITY DEFINER`, and planning its body
    # under `zenith_app` would measure a different query from the one that runs.
    async with engine.connect() as connection:
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        await connection.execute(
            text("SELECT set_config('zenith.tenant_id', :t, true)"), {"t": tenant}
        )
        await connection.execute(
            text("SELECT set_config('zenith.label_ids', :l, true)"), {"l": ",".join(labels)}
        )
        await connection.execute(text("SET LOCAL paradedb.enable_custom_scan = on"))
        arms["lexical"] = await _plan_and_locks(
            connection,
            lexical_arm,
            {"question": "tax", "want": CANDIDATES, "tenant": tenant},
        )
        await connection.rollback()

    await engine.dispose()

    partitioned = bool(shape)
    return {
        "partitioned": partitioned,
        "corpus": corpus,
        "shape": shape,
        "relations_per_parent": relations,
        "relations_per_partition_pair": (
            sum(relations.values()) // max(shape["chunks"]["partitions"], 1) if partitioned else 0
        ),
        "postgres": settings_rows,
        # Recorded so a reader can see which body the plan beside it came from.
        "lexical_search_prunes_at_plan_time": ":tenant AND" in lexical_arm,
        "tenant_measured": tenant,
        "labels_in_context": len(labels),
        "pruning": {"plans": arms},
    }


def _verdict(measured: dict[str, Any], recall: dict[str, Any]) -> dict[str, Any]:
    """The bars, each with the value that met or missed it.

    Written this way round on purpose: a report that prints only the numbers leaves the
    reader to remember what was supposed to happen, and by the time anybody reads it nobody
    does.
    """
    corpus = measured["corpus"]
    shape = measured["shape"]
    arms = measured["pruning"]["plans"]
    checks: dict[str, Any] = {}

    for key, bar in CORPUS_BAR.items():
        checks[f"corpus.{key}"] = {"bar": bar, "measured": corpus[key], "met": corpus[key] == bar}
    for key, bar in RECALL_BAR.items():
        value = recall[key]
        checks[f"recall.{key}"] = {"bar": bar, "measured": value, "met": value >= bar}
    for relation in ("chunks", "chunk_embeddings"):
        found = shape.get(relation, {})
        checks[f"modulus.{relation}"] = {
            "bar": MODULUS_BAR,
            "measured": found.get("partitions"),
            "met": found.get("partitions") == MODULUS_BAR and found.get("modulus") == MODULUS_BAR,
        }
    for arm, result in arms.items():
        scanned = result["partitions_in_plan"]
        checks[f"pruning.{arm}"] = {
            "bar": PARTITIONS_IN_PLAN_BAR,
            "measured": scanned,
            "met": bool(scanned) and max(scanned.values()) <= PARTITIONS_IN_PLAN_BAR,
        }
        # The bar that stops the one above from passing on an empty result. An arm that
        # returns no rows removes every subplan and has measured nothing.
        checks[f"rows.{arm}"] = {
            "bar": ROWS_BAR,
            "measured": result["rows"],
            "met": result["rows"] >= ROWS_BAR,
        }
    checks["locks.lexical"] = {
        "bar": LEXICAL_LOCK_BAR,
        "measured": arms["lexical"]["locks_held_after"],
        "met": arms["lexical"]["locks_held_after"] <= LEXICAL_LOCK_BAR,
    }

    return {"checks": checks, "all_met": all(check["met"] for check in checks.values())}


async def _run() -> int:
    measured = await _measure()
    where = await installation()
    recall = await score(where)
    verdict = _verdict(measured, dict(recall))

    report = {
        "bars": {
            "corpus": CORPUS_BAR,
            "recall": RECALL_BAR,
            "modulus": MODULUS_BAR,
            "partitions_in_plan": PARTITIONS_IN_PLAN_BAR,
            "superseded_subplans_removed_per_append": SUPERSEDED_SUBPLANS_BAR,
            "lexical_locks": LEXICAL_LOCK_BAR,
            "rows_per_arm": ROWS_BAR,
        },
        "verdict": verdict,
        "recall": recall,
        **measured,
    }
    REPORT.write_text(json.dumps(report, indent=2, default=str) + "\n")

    for name, check in verdict["checks"].items():
        mark = "ok  " if check["met"] else "FAIL"
        print(f"{mark} {name:<32} {check['measured']} / {check['bar']}")
    print(f"\nWritten to {REPORT.name}")
    return 0 if verdict["all_met"] else 1


def run() -> int:
    return asyncio.run(_run())
