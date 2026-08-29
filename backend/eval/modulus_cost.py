# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""What the HASH modulus costs, on both sides, plotted against each other.

`eval/partition-shape.json` recommended MODULUS 256, and it chose that number against a
ceiling: the shared lock table runs out during *planning* and the query never runs. 256 was
"as many as we can safely afford". `eval/lock-budget.json` then sized
`max_locks_per_transaction`, verified 32 concurrent queries at 256 partitions, and **took
that ceiling away**. The modulus is now free to be chosen on its merits, and nobody had
plotted what its merits are.

There are two of them and they move in opposite directions.

**Planning grows with the modulus.** A query over a partitioned pair builds paths for every
partition and every index on each before runtime pruning discards them, so planning is
linear in the modulus. `lock-budget.json` measured 55.5 ms at 256 against 12.6 ms at 64 on
the dense half alone, and showed the prepared-statement path does not amortise it: psycopg
promotes the statement and Postgres then builds **11 custom plans and 0 generic ones**, so
the cost is paid per execution rather than once per connection.

**Widening shrinks with the modulus.** HASH on `tenant_id` does not split a tenant; it seats
other tenants beside it. A query for tenant *t* reads the partition *t* hashed into, which
also holds every other tenant that hashed to the same remainder. With T tenants and modulus
P, a tenant shares its partition with (T-1)/P others in expectation, and what it reaches is
its own rows plus theirs.

The question this file answers is where the sum of those two is smallest, **and whether the
sum has a minimum worth caring about or is flat over a wide range.** A flat answer is a
result. A spurious optimum quoted to three figures is not.

## The bar, set before the run

Written here first so the result cannot be read backwards afterwards. A failed bar stays in
this file rather than being rewritten; see `eval/coarse.py`, where one did.

**Bar 1 — the curve has to move.** Planning at the top rung must exceed planning at the
bottom partitioned rung by at least `CURVE_MOVED`, and every partitioned rung must report
`Subplans Removed = P - 1` on both `Append` nodes of the dense join. A sweep whose planning
is flat, or which pruned nothing, measured its own harness rather than the modulus. Three
null results this week were indistinguishable from experiments that never ran — including a
"37x prepared-statement regression" that turned out to be a `text` placeholder inside a
`CAST(... AS halfvec(1024))`, the harness timing its own cast.

**Bar 2 — the two routes to widening must agree.** Widening is measured twice by different
means: rows counted in the queried tenant's partition from the catalogue, and rows predicted
by reproducing Postgres's partition hash in arithmetic. They must agree exactly, at every
rung, and the reproduced hash must also agree with `satisfies_hash_partition` on every tenant
in play. A model that disagrees with the schema is not a rule, and a rule with no run behind
it is worse than no rule.

**Bar 3 — the arms must be the same query.** Every arm must return rows, and the two lexical
arms — the function as migration 0022 ships it, and the plpgsql-local form
`unpruned-plpgsql-pruning.sql` measures — must return the same chunk ids at the same ranks
with no NULL score. ADR 0002's F18 is a plan that stops running ParadeDB's custom scan,
returns NULL from every `paradedb.score()`, ranks everything equally and keeps answering. A
timing taken across that boundary compares two different products.

**Bar 4 — a recommendation needs a stated flat region.** The report must name the range of
moduli whose total is within `FLAT_TOLERANCE` of the minimum, and that tolerance is declared
here rather than chosen once the shape is known. If more than one rung is inside it, the
answer is "flat over this range" and no single optimum may be quoted.

## Which system is being measured

Two curves are possible and they do not agree, so this says which one it plots.

`zenith_lexical_search` is `SECURITY DEFINER` and its tenant clause lives inside the Tantivy
operand, where it is not a partition-key qualifier. It prunes **nothing**:
`unpruned-queries.json` measured 81.5 ms of planning at 256 partitions against 0.25 ms
unpartitioned. `unpruned-plpgsql-pruning.sql` measured the fix — the tenant read into a
plpgsql local, which becomes a parameter the planner may fold — at 32 partitions going from
32 partitions planned to 1, and 3.341 ms to 0.528 ms, with identical results.

**The headline curve here is the system after that fix lands**, because the modulus is being
chosen for the system that will exist rather than the one mid-migration. Both are measured
and both are in the report, as `lexical.pruned` and `lexical.shipped`, so that if they point
at different optima the difference is visible rather than assumed away.

This installation's configured engine is a third case again — `ZENITH_LEXICAL_ENGINE`
defaults to `tsvector`, which is an ordinary policy-scoped statement that prunes at executor
startup like the dense half. It is measured too, because it is what `latency.json`'s 50.26 ms
lexical stage actually timed.

## What this installation can and cannot settle

13,549 passages and two real tenants. `eval/partition-shape.json`'s caveats apply here
unchanged: **planning time, `Subplans Removed`, lock counts and row placement are properties
of the plan, the catalogue and the hash, and they transfer. Milliseconds of execution on this
machine do not** — 13,549 passages over 200 tenants is 68 rows a tenant, and no execution
time measured there says anything about 322 million. So widening is carried as a **ratio**
and never converted to milliseconds, and the rule at the end rests on the ratio.

The size distribution is an assumption, labelled as one, exactly as `partition-shape.json`
labels its own. What is not an assumption is that skew exists: this installation's larger
tenant holds 0.6106 of the corpus against the 0.5 a uniform split would give it.

## Read-only with respect to the corpus

Everything is built inside `zenith_modulus`, dropped in a `finally`, and verified gone.
`chunks` and `chunk_embeddings` are read for their rows and never written; the counts are
re-checked at the end and reported. The teardown drops in batches with a per-table fallback,
because `DROP SCHEMA ... CASCADE` over a thousand partitions is one transaction taking an
`AccessExclusiveLock` on every object in it, and it fails under exactly the pressure this
file exists to measure — which is how a neighbouring branch left 2,075 relations behind.

    docker compose exec -T api python -m eval modulus-cost [--partitions 32,64,256]
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

REPORT = Path(__file__).parent / "modulus-cost.json"

#: One schema, one name, dropped in a `finally`, so a leftover says who left it.
SCHEMA = "zenith_modulus"

#: The ladder. `None` is the unpartitioned pair the product ran before migration 0026 — the
#: left endpoint of both curves, and the only rung where widening is at its maximum and
#: planning at its minimum. Powers of two from 32, because a HASH modulus that is not a power
#: of two distributes no better and makes the `MODULUS`/`REMAINDER` arithmetic harder to read.
RUNGS: tuple[int | None, ...] = (None, 32, 64, 128, 256, 512, 1024)

#: Synthetic tenant populations the corpus is redistributed over. 8 is what
#: `partition-shape.json` and `lock-budget.json` both used, so their numbers and these can be
#: read beside each other; it is also the shape of a small installation, where the modulus
#: stands far above the tenant count and most partitions are empty. 200 is the tenant count
#: the capacity plan carries. The pair brackets the question the rule has to answer.
POPULATIONS: tuple[int, ...] = (8, 200)

#: The skew the corpus is redistributed under. `partition-shape.json`'s `benefit.models`
#: carries Zipf 0.8/1.0/1.2 as assumptions with no measurement behind them, and 1.0 is its
#: middle. It is an assumption here too.
ZIPF = 1.0

#: Repeats of the planning arm. Eleven, because the brief asks for a tail as well as a median
#: and the tail is what a reader feels; eleven readings give a median at position 6 and a p95
#: at position 10 without either being the single worst sample. Eleven is also past both
#: thresholds that matter to a prepared statement: psycopg promotes on its sixth execution
#: and Postgres wants five custom plans before it will consider a generic one, so an arm that
#: switches has room to show it — and the per-execution series is kept for that reason.
PLAN_REPEATS = 11

#: Repeats of the whole-request arm. Fewer, because each is four round trips rather than one.
REQUEST_REPEATS = 9

#: Partitions created, and dropped, per statement. Every `CREATE TABLE ... PARTITION OF`
#: holds its locks to the end of its transaction, so an unbatched build runs out of the same
#: lock table this file is measuring and fails during setup — which looks like a result and
#: is not one.
BATCH = 16

#: Independent draws of a tenant population for the widening model. Which tenants collide is
#: decided by a hash of a UUID nobody chose, so a single population is a single sample of
#: that luck. Forty draws give the spread as well as the mean, which matters because the
#: number quoted back at an operator is the worst tenant's rather than the average one's.
DRAWS = 40

#: Tenant counts the widening model is evaluated at. Wider than `POPULATIONS`, because the
#: model is arithmetic over the real hash and costs nothing, and the rule has to hold at the
#: 20-tenant installation as well as the 200-tenant one.
MODELLED: tuple[int, ...] = (5, 10, 20, 50, 100, 200, 500, 1000)

#: Distributions the model is evaluated under. `0.0` is uniform, which is the assumption a
#: plan reaches for by default; the gap between that row and the others is how wrong the
#: default is.
EXPONENTS: tuple[float, ...] = (0.0, 0.8, 1.0, 1.2)

#: How far above the minimum a rung may sit and still count as inside the flat region.
#: Declared before the run — see Bar 4. Ten per cent of the *total*, not of the planning term
#: alone: a tolerance on the smaller of two addends would call every rung distinct and
#: manufacture the optimum this file exists to test for.
FLAT_TOLERANCE = 0.10

#: The ratio at which the planning curve counts as having moved. See Bar 1.
CURVE_MOVED = 4.0

#: `HASH_PARTITION_SEED` from `src/include/partitioning/partbounds.h`, and the constant
#: `hash_combine64` adds. Reproducing the partition hash in arithmetic is what lets the
#: widening model be evaluated at tenant counts this installation cannot build — and it is
#: checked against `satisfies_hash_partition` at every built rung rather than trusted,
#: because a hash that is subtly wrong produces an entirely plausible curve.
HASH_SEED = 8816678312871386365
HASH_COMBINE = 5305509591434766563
TWO64 = 18446744073709551616

#: What the dense half asks for. Imported rather than repeated.
WANTED = CANDIDATES


def _literal(value: str) -> str:
    """A string as a SQL literal, for the one place a bind parameter is not allowed.

    `EXECUTE` takes expressions rather than protocol parameters, so the two prepared-plan
    arms have to write their arguments in. Every value that reaches here is written in this
    file — a fixed question and a synthetic tenant UUID — and the doubling is here so that
    stays true if someone later passes something that is not.
    """
    return "'" + value.replace("'", "''") + "'"


def _remainder(expr: str, modulus: int) -> str:
    """Postgres's own partition hash for a `uuid`, as an expression over `expr`.

    `uuid_hash_extended(v, HASH_PARTITION_SEED)` seeded exactly as `compute_partition_hash`
    seeds it, then `hash_combine64(0, h)`, which for a zero accumulator is an addition, then
    the modulus. Signed-to-unsigned by hand, because `bigint` is signed and a hash is not.
    """
    hashed = f"uuid_hash_extended({expr}, CAST({HASH_SEED} AS bigint))"
    unsigned = (
        f"(CAST({hashed} AS numeric) "
        f"+ CASE WHEN {hashed} < 0 THEN CAST({TWO64} AS numeric) ELSE 0 END)"
    )
    combined = f"mod({unsigned} + CAST({HASH_COMBINE} AS numeric), CAST({TWO64} AS numeric))"
    return f"CAST(mod({combined}, {modulus}) AS int)"


# --- Reading a plan ----------------------------------------------------------------------

_PLANNING = re.compile(r"Planning Time: ([\d.]+) ms")
_EXECUTION = re.compile(r"Execution Time: ([\d.]+) ms")
_REMOVED = re.compile(r"Subplans Removed: (\d+)")
#: A scan node naming one of this schema's partitions — `e17`, `c204` — and never the parent,
#: whose relations are `emb` and `chk`.
_PARTITION = re.compile(r"\bon ([ec]\d+)\b")


def _read_plan(lines: list[str]) -> dict[str, Any]:
    """What a partitioned plan has to state about itself.

    `Subplans Removed` appears only under `ANALYZE`: pruning on a `STABLE` key happens at
    executor startup, so a plan that was never started cannot report it. Collected as a list,
    one entry per `Append`, because a single number would report one relation's pruning as
    the whole query's — and the dense join has two partitioned relations.
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
        "appends": len(removed),
        "scan_nodes": len(scans),
        # The direct count, and for the unpruned arm it is the only one there is.
        # `Subplans Removed` is reported by an `Append` that pruned at executor startup; a
        # plan that pruned at *plan* time has no `Append` to report it, and a plan that
        # pruned nothing has nothing to report either — so the two opposite outcomes both
        # print an empty list. Counting the partitions the plan actually names separates
        # them: 1 is plan-time pruning, P is no pruning at all.
        "partitions_in_plan": len(_PARTITION.findall(joined)),
        "leaf_scans": scans[:4],
    }


def _spread(values: list[float]) -> dict[str, float]:
    """Median, tail and floor of a set of readings, in the order they were taken.

    The tail is reported because it is what a reader feels, and because a cost that is cheap
    in the median and expensive at p95 is a different product from one that is uniformly
    slow. Over eleven samples the p95 *is* the maximum, so `max` is stated rather than
    implied.

    `*_warm` drops the first reading. The first plan on a connection is measurably dearer —
    the catalogue rows for a few thousand partitions have to be read before anything can be
    planned — and it is a real cost a pooled connection pays once, not once per query. Both
    are reported because neither alone is the answer: the cold figure over-reports a steady
    state and the warm figure under-reports the first query a connection serves.
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
        "p95_warm": round(warm[min(len(warm) - 1, int(len(warm) * 0.95))], 3),
        "first": round(values[0], 3),
    }


def _median_of(arm: dict[str, Any], field: str = "planning_ms") -> float:
    return float((arm.get(field) or {}).get("median") or 0.0)


def _tail_of(arm: dict[str, Any], field: str = "planning_ms") -> float:
    return float((arm.get(field) or {}).get("p95") or 0.0)


def _collapse(series: list[float]) -> int | None:
    """Where a prepared statement stopped planning, or `None` if it never did.

    Postgres will consider a generic plan after five custom ones, and a generic plan is built
    once and then reused — so a statement that adopts one stops reporting planning time at
    all, dropping from tens of milliseconds to thousandths. The step is unmistakable and it
    is found rather than thresholded, because an absolute threshold gets it wrong at exactly
    the rungs where the numbers are largest: 0.011 ms is a collapse beside 55 ms and would be
    read as an ordinary reading by any fixed floor above it.

    Read off the stored series rather than decided at measurement time, so that a rung
    measured in an earlier pass is re-read by the same rule as one measured in this pass.
    """
    if len(series) < 3:
        return None
    reference = statistics.median(series[: min(5, len(series))])
    for index, value in enumerate(series):
        if index and value < 0.1 * reference:
            return index
    return None


def _planning_of(arm: dict[str, Any]) -> float:
    """What one execution of this statement pays to be planned.

    The median of the readings that were actually plans. Repeating an identical statement
    eleven times is enough for Postgres to adopt a generic plan and stop planning, which drags
    the plain median towards zero and would report a cost the product does not get.
    `lock-budget.json` measured the product's own path and found **11 custom plans and 0
    generic ones** over eleven executions of the dense query — psycopg prepares, the planner
    keeps choosing custom, and the planning is paid every time. So where an arm here
    collapsed, the pre-collapse median is the one that transfers, and the collapse is reported
    beside it rather than hidden.
    """
    series = [float(value) for value in (arm.get("planning_series_ms") or [])]
    if not series:
        return _median_of(arm)
    index = _collapse(series)
    return round(statistics.median(series[:index] if index else series), 3)


async def _explain(conn: AsyncConnection, statement: str, params: dict[str, object]) -> list[str]:
    rows = await conn.execute(
        text(f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY ON) {statement}"), params
    )
    return [str(row[0]) for row in rows]


async def _locks(conn: AsyncConnection) -> int:
    """Locks held by this backend right now, counted inside the transaction that took them.

    Every one is released at commit, so this is the only place the number exists. Taking a
    difference against a reading from before the statement removes the virtual transaction id
    and the other per-transaction constants.
    """
    return int(
        (
            await conn.execute(text("SELECT count(*) FROM pg_locks WHERE pid = pg_backend_pid()"))
        ).scalar_one()
    )


# --- The queries, as the product writes them ---------------------------------------------


def _dense(schema: str) -> str:
    """`dense()` from `app/features/retrieval/search.py`, against the scratch pair.

    The join is never dropped. `chunk_embeddings` is filtered by tenant only, so the join to
    `chunks` is where label isolation is enforced for the dense half, and a measurement
    without it measures a query this product must never run.
    """
    return (
        "SELECT c.id, 1 - (e.embedding_half <=> CAST(:embedding AS halfvec(1024))) AS score "
        f"FROM {schema}.emb e "
        f"JOIN {schema}.chk c ON c.id = e.chunk_id "
        "WHERE e.embedding_model = :model AND e.embedding_version = :version "
        "ORDER BY e.embedding_half <=> CAST(:embedding AS halfvec(1024)) LIMIT :limit"
    )


def _tsvector(schema: str) -> str:
    """`lexical()` under the engine this installation is configured with.

    `ZENITH_LEXICAL_ENGINE` defaults to `tsvector`, so this — not `zenith_lexical_search` — is
    the statement behind `latency.json`'s 50.26 ms lexical stage. It is an ordinary
    policy-scoped `SELECT`, so it prunes at executor startup and pays the planning.

    The `tsquery` arrives already tokenised, as a parameter, because the product tokenises in
    a round trip of its own — `lexical.to_tsquery` — and folding that into this statement
    would measure a query the product does not send. The round trip is timed separately in
    the request arm.
    """
    return (
        f"SELECT c.id, ts_rank_cd(c.tsv, q) AS score FROM {schema}.chk c, "
        "to_tsquery(CAST('zenith_text' AS regconfig), CAST(:query AS text)) q "
        "WHERE c.tsv @@ q "
        "ORDER BY score DESC, c.id LIMIT :limit"
    )


def _hydrate(schema: str) -> str:
    """`hydrate()`: the last statement of a request, and the last visit to `chunks`."""
    return f"SELECT c.id, c.text FROM {schema}.chk c WHERE c.id = ANY(:ids)"


#: `lexical.to_tsquery`: the product's own tokenisation round trip, ORing the lexemes because
#: a question is not a filter. Copied rather than imported so that what is timed is a
#: statement rather than a call through a session wrapper.
_TOKENISE = (
    "SELECT array_to_string(tsvector_to_array(to_tsvector("
    "CAST('zenith_text' AS regconfig), CAST(:question AS text))), ' | ')"
)

#: `identifiers.exact`: the same shape ANDed rather than ORed. It exists in the request arm
#: for its locks and its round trip — it is the third visit to `chunks` — and it is not held
#: to Bar 3, because on most questions the product does not run it at all.
_EXACT_JOIN = " & "


async def _context(conn: AsyncConnection, tenant: str, labels: str = "") -> None:
    """The session the application would have, set the way `set_rls_context` sets it.

    Identical but for the timeout, which is raised well above the product's 10 s: planning
    over a thousand partitions can approach it, and a rung that timed out would be reported
    as a failure to plan rather than as the planning cost it is.

    A parallel plan over thousands of partitions asks for a shared memory segment larger than
    this container's 1 GB `/dev/shm` and dies with `DiskFull`, which names the wrong resource
    entirely. Serial here, as in `eval/partition_shape.py`, and for the same reason.
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
    await conn.execute(text("SET LOCAL paradedb.enable_custom_scan = on"))


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
  text         varchar NOT NULL,
  tsv          tsvector GENERATED ALWAYS AS
                 (to_tsvector('zenith_text'::regconfig, text::text)) STORED,
  unlabelled   boolean GENERATED ALWAYS AS (label_ids = '{}'::uuid[]) STORED
"""

#: The installation's index set, read off `pg_indexes` on `chunks` and `chunk_embeddings` and
#: replayed on the parents so Postgres propagates one copy to every partition. Written out
#: rather than generated, for the two reasons `eval/lock_budget.py` records: a unique
#: constraint on a partitioned table must contain the partition key, and the bm25 index has to
#: be exhibited in full so that what is counted is one relation rather than a schema of them.
_INDEX_DDL = (
    "ALTER TABLE {schema}.emb ADD PRIMARY KEY (chunk_id, embedding_model, "
    "embedding_version, tenant_id)",
    "CREATE INDEX ON {schema}.emb USING hnsw (embedding_half halfvec_cosine_ops) "
    "WITH (m = 16, ef_construction = 64)",
    "ALTER TABLE {schema}.chk ADD PRIMARY KEY (id, tenant_id)",
    "CREATE INDEX ON {schema}.chk USING bm25 (id, text, tenant_id, label_ids, unlabelled) "
    'WITH (key_field = id, text_fields = \'{{"text": {{"tokenizer": '
    '{{"type": "stem", "language": "Spanish", "lowercase": true}}}}}}\')',
    "CREATE INDEX ON {schema}.chk USING gin (label_ids)",
    "CREATE INDEX ON {schema}.chk (tenant_id)",
    "CREATE INDEX ON {schema}.chk USING gin (tsv)",
)

_POLICY_DDL = (
    "ALTER TABLE {schema}.emb ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE {schema}.chk ENABLE ROW LEVEL SECURITY",
    "CREATE POLICY emb_isolation ON {schema}.emb USING (tenant_id = zenith_current_tenant())",
    "CREATE POLICY chk_isolation ON {schema}.chk "
    "USING (tenant_id = zenith_current_tenant() "
    "AND (label_ids = '{{}}' OR label_ids && zenith_current_labels()))",
    "GRANT SELECT ON {schema}.emb, {schema}.chk TO zenith_app",
)

#: The lexical function as migration 0022 ships it, and the form
#: `unpruned-plpgsql-pruning.sql` measured. Two differences and no others between them: the
#: tenant is read into a plpgsql local first, and that local also appears as an ordinary SQL
#: qualifier *beside* the Tantivy term rather than instead of it. Moving the term out is ADR
#: 0002's F18 — the custom scan stops running, every score is NULL, everything ranks equally
#: and search keeps answering, worse and silently.
#:
#: A tenant *parameter* on the signature would be the tempting version of this and would be a
#: leak: the function runs as its owner, no policy applies, and its tenant clause is the only
#: thing between one customer and another's passages. The value keeps coming from the GUC.
_FUNCTION_DDL = """
CREATE FUNCTION {schema}.lexical_shipped(query_string text, want integer)
RETURNS TABLE(chunk_id uuid, score real)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = {schema}, public, paradedb
SET paradedb.enable_custom_scan = on
AS $body$
BEGIN
    IF zenith_current_tenant() IS NULL THEN RETURN; END IF;
    RETURN QUERY
    SELECT c.id, paradedb.score(c.id)
    FROM {schema}.chk c
    WHERE c.id @@@ paradedb.boolean(must => ARRAY[
            paradedb.match('text', query_string),
            paradedb.term('tenant_id', zenith_current_tenant()),
            paradedb.boolean(should => ARRAY[paradedb.term('unlabelled', true)])])
    ORDER BY paradedb.score(c.id) DESC
    LIMIT want;
END;
$body$;
CREATE FUNCTION {schema}.lexical_pruned(query_string text, want integer)
RETURNS TABLE(chunk_id uuid, score real)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = {schema}, public, paradedb
SET paradedb.enable_custom_scan = on
AS $body$
DECLARE
    v_tenant uuid := zenith_current_tenant();
BEGIN
    IF v_tenant IS NULL THEN RETURN; END IF;
    RETURN QUERY
    SELECT c.id, paradedb.score(c.id)
    FROM {schema}.chk c
    WHERE c.tenant_id = v_tenant
      AND c.id @@@ paradedb.boolean(must => ARRAY[
            paradedb.match('text', query_string),
            paradedb.term('tenant_id', v_tenant),
            paradedb.boolean(should => ARRAY[paradedb.term('unlabelled', true)])])
    ORDER BY paradedb.score(c.id) DESC
    LIMIT want;
END;
$body$;
"""

#: `EXPLAIN` on a call does not descend into a plpgsql body — it reports `Function Scan` and
#: one number — so the two plans are exhibited through `PREPARE`/`EXECUTE`. That is not an
#: approximation of what plpgsql does: a plpgsql statement *is* an SPI prepared plan whose
#: locals are its parameters, and it obeys `plan_cache_mode` exactly as these do. The
#: functions above are still what the equivalence check calls, so the correctness half is
#: measured on the real thing.
_PREPARE_SHIPPED = """
PREPARE shipped_plan(text, int) AS
SELECT c.id, paradedb.score(c.id)
FROM {schema}.chk c
WHERE c.id @@@ paradedb.boolean(must => ARRAY[
        paradedb.match('text', $1),
        paradedb.term('tenant_id', zenith_current_tenant()),
        paradedb.boolean(should => ARRAY[paradedb.term('unlabelled', true)])])
ORDER BY paradedb.score(c.id) DESC
LIMIT $2
"""

_PREPARE_PRUNED = """
PREPARE pruned_plan(uuid, text, int) AS
SELECT c.id, paradedb.score(c.id)
FROM {schema}.chk c
WHERE c.tenant_id = $1
  AND c.id @@@ paradedb.boolean(must => ARRAY[
        paradedb.match('text', $2),
        paradedb.term('tenant_id', $1),
        paradedb.boolean(should => ARRAY[paradedb.term('unlabelled', true)])])
ORDER BY paradedb.score(c.id) DESC
LIMIT $3
"""


# --- Building and tearing down -----------------------------------------------------------


async def _drop_tables(conn: AsyncConnection, keep: tuple[str, ...] = ()) -> int:
    """Drop the schema's tables in batches, with a one-at-a-time fallback.

    `DROP SCHEMA ... CASCADE` over a thousand partitions is one transaction taking an
    `AccessExclusiveLock` on every object in it, which is more locks than the shared table
    holds at the top of this ladder. The tidy-up then fails and the schema survives, which is
    the one outcome this file may not produce — a neighbouring branch left 2,075 relations
    behind exactly this way. The fallback matters as much as the batching: under real
    pressure even sixteen is too many, and a teardown that gives up under the pressure it is
    measuring is not a teardown.

    **A partitioned parent is `relkind = 'p'`, not `'r'`**, and the first full run of this
    sweep missed it: every rung after the first partitioned one failed with
    `relation "emb" already exists`, because the parents survived a teardown that only looked
    for ordinary tables. It cost a run and nothing else, because the failure was loud —
    twelve rungs reported an error instead of a number. Had the parents merely been *stale*
    rather than in the way, the ladder would have measured the first rung twelve times.

    `ORDER BY c.relkind DESC` puts `'r'` before `'p'` so partitions go first. Dropping a
    parent cascades to every partition under it in one transaction, which is the lock
    explosion the batching exists to avoid.
    """
    dropped = 0
    excluded = " AND c.relname <> ALL(:keep)" if keep else ""
    while True:
        names = [
            str(row[0])
            for row in await conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid = c.relnamespace WHERE n.nspname = :s "
                    "AND c.relkind IN ('r', 'p')"
                    f"{excluded} ORDER BY c.relkind DESC LIMIT :n"
                ),
                {"s": SCHEMA, "n": BATCH, **({"keep": list(keep)} if keep else {})},
            )
        ]
        if not names:
            return dropped
        targets = ", ".join(f"{SCHEMA}.{name}" for name in names)
        try:
            await conn.execute(text(f"DROP TABLE IF EXISTS {targets} CASCADE"))
            dropped += len(names)
            continue
        except Exception:  # noqa: BLE001 - the fallback is the point of this function
            pass
        for name in names:
            try:
                await conn.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.{name} CASCADE"))
                dropped += 1
            except Exception:  # noqa: BLE001, PERF203 - one stuck table must not stop the rest
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
        for name in ("lexical_shipped", "lexical_pruned"):
            await conn.execute(
                text(f"DROP FUNCTION IF EXISTS {SCHEMA}.{name}(text, integer) CASCADE")
            )
        await _drop_tables(conn, keep=("assign",))


async def _assign(owner: AsyncEngine, tenants: int, space: tuple[str, str]) -> dict[str, object]:
    """One table saying which synthetic tenant each real chunk belongs to.

    Written once per population and reused by every modulus rung, so the rows in a given
    tenant are the same rows at 64 partitions and at 1024, and the comparison is of moduli and
    nothing else.

    The draw is Zipf over the tenant index rather than uniform, and that is the point.
    `partition-shape.json` refuted the uniform assumption with the one datum two tenants can
    supply — the larger holds 0.6106 of the corpus, not 0.5 — and a widening curve measured
    over a uniform population would answer a question no installation asks. Which chunk goes
    where is `md5(chunk_id)` through the inverse CDF, so it is deterministic and identical
    across rungs.
    """
    async with owner.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        # Without this the application role cannot see the schema at all and every arm
        # returns `permission denied` — which the first run of this sweep recorded as an
        # error per rung rather than as a measurement, and which is the cheapest possible
        # example of a null result that looks like a finding.
        await conn.execute(text(f"GRANT USAGE ON SCHEMA {SCHEMA} TO zenith_app"))
        await conn.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.assign"))
        await conn.execute(
            text(
                f"CREATE TABLE {SCHEMA}.assign AS "
                "WITH w AS (SELECT i, 1.0 / power(i, :zipf) AS wt "
                "           FROM generate_series(1, :t) AS i), "
                "     c AS (SELECT i, (sum(wt) OVER (ORDER BY i)) "
                "                     / (SELECT sum(wt) FROM w) AS cum FROM w), "
                "     u AS (SELECT chunk_id, "
                "                  (('x' || substr(md5(chunk_id::text), 1, 8))::bit(32)::bigint "
                "                    & 2147483647)::numeric / 2147483648.0 AS r "
                "           FROM chunk_embeddings "
                "           WHERE embedding_model = :model AND embedding_version = :version) "
                "SELECT u.chunk_id, "
                "       ('00000000-0000-0000-0000-' "
                "        || lpad((SELECT min(c.i) FROM c WHERE c.cum >= u.r)::text, 12, '0'))"
                "         ::uuid AS tenant_id "
                "FROM u"
            ),
            {"zipf": ZIPF, "t": tenants, "model": space[0], "version": space[1]},
        )
        await conn.execute(text(f"ALTER TABLE {SCHEMA}.assign ADD PRIMARY KEY (chunk_id)"))
        rows = list(
            await conn.execute(
                text(
                    f"SELECT tenant_id::text AS tid, count(*) AS n FROM {SCHEMA}.assign "
                    "GROUP BY 1 ORDER BY 2 DESC"
                )
            )
        )
    total = sum(int(row.n) for row in rows)
    middle = rows[len(rows) // 2]
    return {
        "requested": tenants,
        "populated": len(rows),
        "rows": total,
        "zipf": ZIPF,
        "largest": str(rows[0].tid),
        "largest_rows": int(rows[0].n),
        "largest_share": round(int(rows[0].n) / total, 4),
        "median": str(middle.tid),
        "median_rows": int(middle.n),
        "smallest_rows": int(rows[-1].n),
        "top_ten": [{"tenant": str(row.tid), "rows": int(row.n)} for row in rows[:10]],
    }


async def _build(owner: AsyncEngine, modulus: int | None, space: tuple[str, str]) -> float:
    """Everything for one rung, in autocommit so no transaction accumulates the lock table.

    Every partition gets `ENABLE ROW LEVEL SECURITY` and a policy of its own in the same batch
    as its `CREATE TABLE`. **A partition inherits neither**: CLAUDE.md's first invariant
    records it and `eval/partition-rls.sql` demonstrates it against this database, where the
    parent answers correctly while a partition addressed directly hands back another tenant in
    full. It is not decoration for a planning measurement either — a policy on a partition is
    an expression the planner fetches and applies for every unpruned child, so a ladder built
    without them would understate planning at exactly the rung where planning is what is
    being watched.
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
                        f"       || ' USING ({emb_policy}) WITH CHECK ({emb_policy})'; "
                        f"  EXECUTE 'CREATE TABLE {SCHEMA}.c' || i "
                        f"       || ' PARTITION OF {SCHEMA}.chk "
                        f"FOR VALUES WITH (MODULUS {modulus}, REMAINDER ' || i || ')'; "
                        f"  EXECUTE 'ALTER TABLE {SCHEMA}.c' || i "
                        "       || ' ENABLE ROW LEVEL SECURITY'; "
                        f"  EXECUTE 'CREATE POLICY c' || i || '_isolation ON {SCHEMA}.c' || i "
                        f"       || ' USING ({chk_policy}) WITH CHECK ({chk_policy})'; "
                        "END LOOP; END $do$;"
                    )
                )
        await _load(conn, space)
    return round(time.perf_counter() - started, 1)


async def _load(conn: AsyncConnection, space: tuple[str, str]) -> None:
    """Route the corpus into whichever partitions hold it, and analyse only those.

    `ANALYZE` on the parent would touch every partition in one transaction and run out of the
    lock table — the same failure `_drop_tables` avoids, from the other direction.
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
            "SELECT a.chunk_id, c.document_id, a.tenant_id, '{}'::uuid[], c.text "
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


async def _relations(owner: AsyncEngine) -> int:
    """Relations in the scratch schema, tables and indexes alike, less `assign` and its key."""
    async with owner.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        count = int(
            (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_class c JOIN pg_namespace n "
                        "ON n.oid = c.relnamespace WHERE n.nspname = :s "
                        "AND c.relkind IN ('r', 'i', 'p', 'I') "
                        "AND c.relname NOT LIKE 'assign%'"
                    ),
                    {"s": SCHEMA},
                )
            ).scalar_one()
        )
        await conn.rollback()
    return count


# --- Widening ----------------------------------------------------------------------------


async def _widening(
    owner: AsyncEngine, modulus: int | None, tenant: str, total_rows: int
) -> dict[str, object]:
    """How many rows this tenant's query reaches, against how many it owns.

    Measured twice by different means, because Bar 2 says a rule needs two routes that agree.
    The first counts the rows physically resident in the partition the tenant hashed into,
    read from that partition by name as the owner. The second reproduces Postgres's partition
    hash in arithmetic and sums the rows of every tenant sharing the remainder. They are the
    same number by two routes, and the arithmetic route is what the model at other tenant
    counts rests on — so it has to be shown correct here rather than assumed.

    `satisfies_hash_partition` is asked as well. That is a third route and the authoritative
    one: it is the function the planner itself uses to decide what a partition holds.
    """
    async with owner.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        owned = int(
            (
                await conn.execute(
                    text(f"SELECT count(*) FROM {SCHEMA}.chk WHERE tenant_id = :t"),
                    {"t": tenant},
                )
            ).scalar_one()
        )
        if not modulus:
            await conn.rollback()
            return {
                "modulus": None,
                "rows_owned": owned,
                "rows_reached_counted": total_rows,
                "rows_reached_predicted": total_rows,
                "routes_agree": True,
                "tenants_sharing_the_partition": None,
                "share_of_corpus_reached": 1.0,
                "widening": round(total_rows / owned, 4) if owned else None,
            }

        remainder = int(
            (
                await conn.execute(
                    text(f"SELECT {_remainder('CAST(:t AS uuid)', modulus)}"), {"t": tenant}
                )
            ).scalar_one()
        )
        authoritative = bool(
            (
                await conn.execute(
                    text(
                        "SELECT satisfies_hash_partition("
                        f"CAST('{SCHEMA}.chk' AS regclass), {modulus}, {remainder}, "
                        "CAST(:t AS uuid))"
                    ),
                    {"t": tenant},
                )
            ).scalar_one()
        )
        counted = int(
            (await conn.execute(text(f"SELECT count(*) FROM {SCHEMA}.c{remainder}"))).scalar_one()
        )
        sharing = list(
            await conn.execute(
                text(
                    f"SELECT tenant_id::text AS tid, count(*) AS n FROM {SCHEMA}.chk "
                    f"WHERE {_remainder('tenant_id', modulus)} = :r "
                    "GROUP BY 1 ORDER BY 2 DESC"
                ),
                {"r": remainder},
            )
        )
        await conn.rollback()

    predicted = sum(int(row.n) for row in sharing)
    return {
        "modulus": modulus,
        "remainder": remainder,
        "hash_agrees_with_satisfies_hash_partition": authoritative,
        "rows_owned": owned,
        "rows_reached_counted": counted,
        "rows_reached_predicted": predicted,
        "routes_agree": counted == predicted and authoritative,
        "tenants_sharing_the_partition": len(sharing),
        "share_of_corpus_reached": round(counted / total_rows, 6) if total_rows else None,
        "neighbours": [
            {"tenant": str(row.tid), "rows": int(row.n)}
            for row in sharing
            if str(row.tid) != tenant
        ][:5],
        "widening": round(counted / owned, 4) if owned else None,
    }


def _share(exponent: float, tenants: int) -> float:
    """The largest tenant's share of the corpus under a Zipf exponent. `0` is uniform."""
    weights = [1.0 / (rank**exponent) for rank in range(1, tenants + 1)]
    return weights[0] / sum(weights)


def _reached(share: float, modulus: int) -> float:
    """The share of the corpus a tenant's query reaches, in expectation over the hash.

    `s + (1 - s) / P`. A tenant always reaches its own rows; every other tenant lands in its
    partition with probability `1 / P` independently of size, so what the rest of the corpus
    contributes is its whole weight divided by the modulus. There is no tenant-count term:
    T cancels, which is why the rule below asks for a share and not for a census.

    The consequence worth reading twice is the floor. `(1 - s) / P` goes to zero and `s` does
    not, so **no modulus takes a tenant below its own share of the corpus.** Uniform hashing
    cannot help a tenant that is large on its own, and the largest tenant is the one whose
    latency gets quoted back at you.
    """
    return share + (1 - share) / modulus


async def _model(owner: AsyncEngine) -> dict[str, object]:
    """Widening for tenant counts this installation cannot build, over the real hash.

    Arithmetic rather than measurement, and it is allowed to be arithmetic only because
    `_widening` has just shown the arithmetic reproduces the schema exactly. What it adds is
    the thing one built rung cannot show: **which tenants collide is luck**, decided by a hash
    of a UUID nobody chose, so a single population is one sample of that luck. `DRAWS`
    populations are drawn per cell and the spread reported, because the number quoted back at
    an operator is the worst tenant's rather than the average one's.
    """
    cells: list[dict[str, object]] = []
    async with owner.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        for exponent in EXPONENTS:
            for tenants in MODELLED:
                for modulus in RUNGS:
                    if modulus is None:
                        continue
                    row = (
                        await conn.execute(
                            text(
                                "WITH draws AS ("
                                "  SELECT d, i, gen_random_uuid() AS v, "
                                "         1.0 / power(i, :zipf) AS wt "
                                "  FROM generate_series(1, :draws) d, generate_series(1, :t) i), "
                                "placed AS ("
                                f"  SELECT d, i, wt, {_remainder('v', modulus)} AS r "
                                "  FROM draws), "
                                "widened AS ("
                                "  SELECT d, i, wt, "
                                "         sum(wt) OVER (PARTITION BY d, r) / wt AS w, "
                                "         sum(wt) OVER (PARTITION BY d, r) "
                                "           / sum(wt) OVER (PARTITION BY d) AS share "
                                "  FROM placed) "
                                "SELECT round(avg(w) FILTER (WHERE i = 1)::numeric, 4) "
                                "         AS big_mean, "
                                "       round(max(w) FILTER (WHERE i = 1)::numeric, 4) "
                                "         AS big_worst, "
                                "       round(avg(w)::numeric, 4) AS mean, "
                                "       round((percentile_disc(0.5) WITHIN GROUP "
                                "              (ORDER BY w))::numeric, 4) AS median, "
                                "       round((percentile_disc(0.99) WITHIN GROUP "
                                "              (ORDER BY w))::numeric, 4) AS p99, "
                                "       round(avg(share) FILTER (WHERE i = 1)::numeric, 6) "
                                "         AS big_share, "
                                "       round(avg(share)::numeric, 6) AS mean_share, "
                                "       round((percentile_disc(0.99) WITHIN GROUP "
                                "              (ORDER BY share))::numeric, 6) AS p99_share "
                                "FROM widened"
                            ),
                            {"zipf": exponent, "draws": DRAWS, "t": tenants},
                        )
                    ).one()
                    cells.append(
                        {
                            "distribution": "uniform" if exponent == 0.0 else f"zipf_{exponent}",
                            "tenants": tenants,
                            "modulus": modulus,
                            "largest_tenant_mean": float(row.big_mean),
                            "largest_tenant_worst_draw": float(row.big_worst),
                            "mean_tenant": float(row.mean),
                            "median_tenant": float(row.median),
                            "p99_tenant": float(row.p99),
                            # The same placement read the other way round, and the half that
                            # converts to time. A small tenant's widening *ratio* is
                            # frightening and its absolute cost is not: reaching 156 times
                            # its own corpus, when its own corpus is a thousandth of the
                            # installation, is reaching a fraction of a large table. What a
                            # query pays for is rows, so the share of the corpus its
                            # partition holds is the number that multiplies into
                            # milliseconds — and for the largest tenant it barely moves.
                            "largest_tenant_share_of_corpus": float(row.big_share),
                            "mean_tenant_share_of_corpus": float(row.mean_share),
                            "p99_tenant_share_of_corpus": float(row.p99_share),
                            # The rule, evaluated beside the draws that are supposed to
                            # confirm it. A rule checked only in prose is a rule nobody has
                            # checked; this puts the arithmetic and the sample in the same
                            # row so a disagreement is visible rather than argued.
                            "closed_form_largest_share_of_corpus": round(
                                _reached(_share(exponent, tenants), modulus), 6
                            ),
                            "closed_form_largest_widening": round(
                                _reached(_share(exponent, tenants), modulus)
                                / _share(exponent, tenants),
                                4,
                            ),
                            "closed_form_over_measured": round(
                                _reached(_share(exponent, tenants), modulus) / float(row.big_share),
                                4,
                            )
                            if float(row.big_share)
                            else None,
                        }
                    )
        await conn.rollback()
    return {
        "draws_per_cell": DRAWS,
        "closed_form": (
            "share_of_corpus_reached = s + (1 - s) / P, where s is the tenant's share of the "
            "corpus and P the modulus; widening = that, over s. Checked against the draws in "
            "every cell as `closed_form_over_measured`, which is 1.00 where the sample is "
            "tight and drifts at the low moduli, where the sum over forty draws is dominated "
            "by whether the second-largest tenant happened to collide."
        ),
        "the_rule": (
            "P >= (1 - s) / (epsilon * s) holds a tenant of share s to epsilon extra rows. "
            "It has no tenant-count term: T cancels. And it has a floor — (1 - s) / P goes to "
            "zero while s does not, so no modulus takes a tenant below its own share of the "
            "corpus."
        ),
        "what_widening_means": (
            "rows a tenant's query reaches divided by rows it owns. 1.0 is a partition to "
            "itself; 2.0 is reaching its own corpus twice over. Computed over Postgres's own "
            "partition hash, reproduced in arithmetic and checked against "
            "satisfies_hash_partition at every built rung."
        ),
        "sizes_are_an_assumption": (
            "Zipf over the tenant rank, exponent as labelled; `uniform` is exponent 0. No "
            "customer size distribution exists in this repository. What is measured rather "
            "than assumed is that skew exists: this installation's larger tenant holds 0.6106 "
            "of the corpus against the 0.5 a uniform split would give it."
        ),
        "cells": cells,
    }


# --- One rung ----------------------------------------------------------------------------


async def _plan_arm(
    conn: AsyncConnection, statement: str, params: dict[str, object]
) -> dict[str, object]:
    """One statement, planned and executed `PLAN_REPEATS` times, with the plan kept.

    The plan is kept for every arm and not only the timing, because three null results this
    week were indistinguishable from experiments that never ran. `Subplans Removed` and the
    leaf scans are what say the query did the thing the number is being attributed to.

    The per-execution series is kept beside the summary because a prepared statement can
    change plan underneath it — Postgres will consider a generic plan after five custom ones,
    and a generic plan has no parameter values to prune on. A median taken across that switch
    would report the average of two different products.
    """
    planning: list[float] = []
    execution: list[float] = []
    pruning: list[list[int]] = []
    named: list[int] = []
    plan: dict[str, Any] = {}
    for _ in range(PLAN_REPEATS):
        plan = _read_plan(await _explain(conn, statement, params))
        if plan["planning_ms"] is not None:
            planning.append(float(plan["planning_ms"]))
        if plan["execution_ms"] is not None:
            execution.append(float(plan["execution_ms"]))
        removed = plan["subplans_removed"]
        pruning.append(list(removed) if isinstance(removed, list) else [])
        named.append(int(plan["partitions_in_plan"] or 0))
    returned = len((await conn.execute(text(statement), params)).all())
    # A prepared statement that stops re-planning has adopted a generic plan, and a generic
    # plan has no parameter values to prune on. It is not an anomaly to be smoothed away by a
    # median: it is the difference between paying the planning once per connection and once
    # per query, which is the whole reason the modulus costs anything.
    collapsed = _collapse(planning)
    return {
        "planning_ms": _spread(planning),
        "execution_ms": _spread(execution),
        "planning_series_ms": [round(value, 3) for value in planning],
        "planning_collapsed_to_a_generic_plan": collapsed is not None,
        "planning_collapsed_at_execution": collapsed,
        "planning_median_before_collapse": (
            round(statistics.median(planning[:collapsed]), 3) if collapsed else None
        ),
        "subplans_removed": plan.get("subplans_removed"),
        "subplans_removed_series": pruning,
        "pruning_was_stable": len({tuple(entry) for entry in pruning}) == 1,
        "partitions_in_plan": plan.get("partitions_in_plan"),
        "partitions_in_plan_series": named,
        "appends": plan.get("appends"),
        "leaf_scans": plan.get("leaf_scans"),
        "returned": returned,
    }


async def _lexical_arms(
    owner: AsyncEngine, app: AsyncEngine, tenant: str, question: str
) -> dict[str, object]:
    """The lexical half in the two forms stage 02 has to choose between.

    Timed through `PREPARE`/`EXECUTE`, whose plans `EXPLAIN` can show, and checked for
    equivalence through the functions themselves, whose bodies `EXPLAIN` cannot enter. Both
    halves are needed: the plan says what the modulus costs, and only the function call can
    say whether the cheaper plan is still the same product.

    **The plans are taken on an owner connection, and that is not a convenience.**
    `zenith_lexical_search` is `SECURITY DEFINER`: its body executes as the function's owner,
    so no policy applies to it. The first version of this sweep ran the two `PREPARE` arms as
    `zenith_app` with the policies in force and ParadeDB refused the query outright — *the
    query argument must be wrapped in a `SearchQueryInput::WithIndex` variant* — because a
    policy-qualified scan is not a scan the custom-scan provider can claim. That is a
    different query from the one the product runs, and had it merely been *slower* rather
    than an error, it would have been measured and believed.

    The locks are counted on the application connection instead, through the functions, which
    is where they are actually charged: a `SECURITY DEFINER` call changes the privileges it
    runs with and not the backend or the transaction, so its relation locks join the caller's.

    `paradedb.enable_custom_scan` is on for the session — `_context` sets it — because these
    `PREPARE`s are not inside a function and so do not carry migration 0022's own `SET`.
    Without it ParadeDB answers through an `Index Only Scan` with `@@@` as an index condition,
    which is a different plan and, per ADR 0002, the one whose `paradedb.score()` is NULL.
    """
    locks: dict[str, object] = {}
    for name in ("lexical_shipped", "lexical_pruned"):
        async with app.connect() as conn:
            await _context(conn, tenant)
            baseline = await _locks(conn)
            try:
                rows = int(
                    (
                        await conn.execute(
                            text(f"SELECT count(*) FROM {SCHEMA}.{name}(:q, :n)"),
                            {"q": question, "n": WANTED},
                        )
                    ).scalar_one()
                )
                locks[name] = {"locks": await _locks(conn) - baseline, "returned": rows}
            except Exception as exc:  # noqa: BLE001 - a refusal here is a result
                locks[name] = {"error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"}
            await conn.rollback()

    async with owner.connect() as conn:
        await _context(conn, tenant)
        arms = await _lexical_plans(conn, tenant, question)
        await conn.rollback()
    return {**arms, "locks_from_the_application_role": locks}


async def _lexical_plans(conn: AsyncConnection, tenant: str, question: str) -> dict[str, object]:
    """The two plans and their timings, in the execution context the function has."""
    await conn.execute(text(_PREPARE_SHIPPED.format(schema=SCHEMA)))
    await conn.execute(text(_PREPARE_PRUNED.format(schema=SCHEMA)))

    arms: dict[str, object] = {
        # The arguments are written into the `EXECUTE` as literals rather than bound. Not a
        # shortcut: an `EXECUTE` cannot carry protocol-level parameters at all — Postgres
        # raises `could not determine data type of parameter $1` and a cast around the
        # placeholder does not help — which the first runs of this sweep recorded as a
        # rung-level error rather than as the harness bug it was. It is also the faithful
        # form: `unpruned-plpgsql-pruning.sql` exhibits the same two plans the same way, and
        # the parameter the planner is asked to fold is the prepared statement's own, which
        # is exactly the parameter a plpgsql local becomes.
        "shipped": await _plan_arm(
            conn, f"EXECUTE shipped_plan({_literal(question)}, {WANTED})", {}
        ),
        "pruned": await _plan_arm(
            conn,
            f"EXECUTE pruned_plan({_literal(tenant)}, {_literal(question)}, {WANTED})",
            {},
        ),
    }

    equivalence = (
        await conn.execute(
            text(
                "WITH s AS (SELECT chunk_id, score, "
                "                  row_number() OVER (ORDER BY score DESC, chunk_id) AS rank "
                f"           FROM {SCHEMA}.lexical_shipped(:q, :n)), "
                "     p AS (SELECT chunk_id, score, "
                "                  row_number() OVER (ORDER BY score DESC, chunk_id) AS rank "
                f"           FROM {SCHEMA}.lexical_pruned(:q, :n)) "
                "SELECT (SELECT count(*) FROM s) AS shipped_rows, "
                "       (SELECT count(*) FROM p) AS pruned_rows, "
                "       (SELECT count(*) FROM s WHERE s.score IS NULL) AS shipped_nulls, "
                "       (SELECT count(*) FROM p WHERE p.score IS NULL) AS pruned_nulls, "
                "       (SELECT count(*) FROM s JOIN p USING (chunk_id)) AS in_both, "
                "       (SELECT count(*) FROM s JOIN p USING (chunk_id) "
                "        WHERE s.rank <> p.rank) AS different_rank"
            ),
            {"q": question, "n": WANTED},
        )
    ).one()

    await conn.execute(text("DEALLOCATE shipped_plan"))
    await conn.execute(text("DEALLOCATE pruned_plan"))

    rows = int(equivalence.shipped_rows)
    return {
        **arms,
        "equivalence": {
            "shipped_rows": rows,
            "pruned_rows": int(equivalence.pruned_rows),
            "shipped_null_scores": int(equivalence.shipped_nulls),
            "pruned_null_scores": int(equivalence.pruned_nulls),
            "in_both": int(equivalence.in_both),
            "rows_at_a_different_rank": int(equivalence.different_rank),
            "interchangeable": (
                rows > 0
                and rows == int(equivalence.pruned_rows) == int(equivalence.in_both)
                and int(equivalence.shipped_nulls) == int(equivalence.pruned_nulls) == 0
                and int(equivalence.different_rank) == 0
            ),
        },
    }


async def _rung(
    owner: AsyncEngine, app: AsyncEngine, tenant: str, params: dict[str, object], question: str
) -> dict[str, object]:
    """The database half of one search request, timed and locked, at one modulus.

    One transaction, because `tenant_session` is one transaction for the whole request and
    locks are released at commit — so the union over its statements is what the shared lock
    table is charged, not the sum.

    The lock counts are taken per statement because the interesting claim is that they do not
    add up: a lock is taken per relation and the planner opens every index of every relation
    it plans, so the second, third and fourth statements — all of which revisit `chunks` —
    should take no new locks at all. If they do, every budget downstream of this is wrong.
    """
    # Arm by arm, each allowed to fail on its own. The whole-request `try` this used to be
    # threw away every arm that had already succeeded when a later one failed, and at
    # MODULUS 1024 that meant losing a measured dense join because the `tsvector` statement
    # could not be planned inside five minutes. One statement being unplannable is a finding
    # about that statement, not about the rung.
    out: dict[str, object] = {}
    errors: dict[str, object] = {}

    async def _arm(name: str, run: Any) -> Any:
        try:
            value = await run
            out[name] = value
        except Exception as exc:  # noqa: BLE001 - a failure here is a result, not an abort
            # The statement is carried with the error on purpose. Early runs of this sweep
            # reported `permission denied` and `could not determine data type` with no
            # indication of which of six statements raised them, which made a harness bug look
            # exactly like an unreachable rung.
            errors[name] = {
                "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}",
                "statement": str(getattr(exc, "statement", "") or "")[:400],
            }
            return None
        return value

    async with app.connect() as conn:
        try:
            await _context(conn, tenant)
            baseline = await _locks(conn)
            locks: dict[str, object] = {"baseline": baseline}

            tsquery = str(
                (await conn.execute(text(_TOKENISE), {"question": question})).scalar_one()
            )
            out["tsquery"] = tsquery[:120]
            lexemes: dict[str, object] = {"query": tsquery, "limit": WANTED}
            identifiers: dict[str, object] = {
                "query": _EXACT_JOIN.join(tsquery.split(" | ")[:3]),
                "limit": 10,
            }

            if await _arm("dense", _plan_arm(conn, _dense(SCHEMA), params)) is not None:
                locks["for_dense"] = await _locks(conn) - baseline
            after_dense = await _locks(conn)

            if await _arm("tsvector", _plan_arm(conn, _tsvector(SCHEMA), lexemes)) is not None:
                locks["added_by_tsvector"] = await _locks(conn) - after_dense
            after_tsvector = await _locks(conn)

            ids = [
                row[0]
                for row in await conn.execute(
                    text(_dense(SCHEMA)), {**params, "limit": min(8, WANTED)}
                )
            ]
            if await _arm("hydrate", _plan_arm(conn, _hydrate(SCHEMA), {"ids": ids})) is not None:
                locks["added_by_hydrate"] = await _locks(conn) - after_tsvector
            locks["for_request"] = await _locks(conn) - baseline
            out["locks"] = locks

            # The whole database half of a request, end to end, timed as one unit. The arms
            # above are timed by the server's own clock through `EXPLAIN`, which excludes the
            # round trip; this is what the service waits for, round trips included, and it is
            # the number that adds to embed and rerank.
            async def _request() -> dict[str, float]:
                request: list[float] = []
                for _ in range(REQUEST_REPEATS):
                    started = time.perf_counter()
                    await conn.execute(text(_TOKENISE), {"question": question})
                    await conn.execute(text(_tsvector(SCHEMA)), lexemes)
                    await conn.execute(text(_dense(SCHEMA)), params)
                    await conn.execute(text(_tsvector(SCHEMA)), identifiers)
                    await conn.execute(text(_hydrate(SCHEMA)), {"ids": ids})
                    request.append((time.perf_counter() - started) * 1000)
                return _spread(request)

            await _arm("database_half_ms", _request())
        except Exception as exc:  # noqa: BLE001 - the transaction itself may be gone
            errors["transaction"] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}"
        finally:
            with contextlib.suppress(Exception):
                await conn.rollback()

    await _arm("lexical", _lexical_arms(owner, app, tenant, question))
    if errors:
        out["errors"] = errors
        # Kept as `error` too, so the report's own "did this rung produce a measurement"
        # check keeps working on a rung that produced only some of one.
        out["error"] = "; ".join(sorted(errors))
    return out


# --- The installation itself -------------------------------------------------------------


async def _server(conn: AsyncConnection) -> dict[str, object]:
    """The settings in force, and the modulus the installation is actually running.

    Read rather than assumed. `max_locks_per_transaction` is owned by
    `chore/partition-lock-budget` and is not touched here; what it is at the moment of the run
    decides which rungs are reachable, so it is recorded beside the results rather than quoted
    from that branch's report.
    """
    rows = list(
        await conn.execute(
            text(
                "SELECT name, setting FROM pg_settings WHERE name IN "
                "('max_locks_per_transaction', 'max_connections', 'max_prepared_transactions', "
                " 'plan_cache_mode', 'shared_buffers', 'work_mem', 'enable_partition_pruning', "
                " 'enable_partitionwise_join', 'server_version') ORDER BY name"
            )
        )
    )
    values = {str(row.name): str(row.setting) for row in rows}
    live = list(
        await conn.execute(
            text(
                "SELECT c.relname AS parent, count(i.inhrelid) AS partitions, "
                "       min(pg_get_expr(p.relpartbound, p.oid)) AS one_bound "
                "FROM pg_class c LEFT JOIN pg_inherits i ON i.inhparent = c.oid "
                "LEFT JOIN pg_class p ON p.oid = i.inhrelid "
                "WHERE c.relname IN ('chunks', 'chunk_embeddings') GROUP BY 1 ORDER BY 1"
            )
        )
    )
    return {
        **values,
        "nominal_lock_slots": int(values["max_locks_per_transaction"])
        * (int(values["max_connections"]) + int(values["max_prepared_transactions"])),
        "lock_table_note": (
            "max_locks_per_transaction * (max_connections + max_prepared_transactions) sizes "
            "one table the whole cluster draws from; it is not a per-transaction allowance. "
            "Owned by chore/partition-lock-budget and not modified by this sweep."
        ),
        "live_partitioning": [
            {
                "parent": str(row.parent),
                "partitions": int(row.partitions),
                "one_bound": str(row.one_bound) if row.one_bound else None,
            }
            for row in live
        ],
    }


async def _cluster(owner: AsyncEngine) -> dict[str, object]:
    """The shared lock table, at the moment this rung is measured.

    `max_locks_per_transaction` sizes one table the whole cluster draws from, it takes a
    restart to change, and this sweep does not own it — `chore/partition-lock-budget` does.
    It is read per rung rather than once per run because it moved *during* this work: the
    setting was 1024 when the harness was written and 64 by the time it ran, the `db`
    container having been recreated in between, and a ladder whose rungs were measured under
    two different ceilings is not a ladder.

    `locks_held_cluster_wide` is here for the same reason from the other direction. Two other
    measurement branches are building partitioned schemas on this cluster, one of them had a
    ten-lock statement fail because a neighbour had taken the table, and a rung that failed
    under a neighbour's pressure is noise rather than a result. The two are indistinguishable
    unless the pressure is recorded beside the failure.
    """
    async with owner.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        row = (
            await conn.execute(
                text(
                    "SELECT current_setting('max_locks_per_transaction') AS locks, "
                    "       (SELECT count(*) FROM pg_locks) AS held, "
                    "       (SELECT count(*) FROM pg_stat_activity "
                    "        WHERE state = 'active') AS active, "
                    "       (SELECT count(*) FROM pg_namespace "
                    "        WHERE nspname LIKE 'zenith\\_%' AND nspname <> :s) AS neighbours, "
                    "       pg_postmaster_start_time()::text AS started"
                ),
                {"s": SCHEMA},
            )
        ).one()
        await conn.rollback()
    return {
        "max_locks_per_transaction": int(row.locks),
        "locks_held_cluster_wide": int(row.held),
        "active_backends": int(row.active),
        "neighbouring_scratch_schemas": int(row.neighbours),
        # Recorded, but **not** the crash signal, and the first version of this file used it
        # as one and was wrong. Postgres does not restart the postmaster when a backend is
        # killed: it terminates the other backends, replays the WAL and carries on, so
        # `pg_postmaster_start_time()` is unchanged across a crash the whole cluster went
        # through. `_survived` reads the recovery instead.
        "postmaster_start_time": str(row.started),
    }


#: What only a cluster that has crashed and is replaying says. `the database system is in
#: recovery mode` is refused at connection time by a postmaster that has just terminated
#: every backend; the other two are what the client sees when its own backend is the one that
#: was terminated. No ordinary query error produces any of them.
_CRASHED = (
    "the database system is in recovery mode",
    "server closed the connection unexpectedly",
    "terminating connection due to",
)


async def _survived(owner: AsyncEngine, error: str, before: str | None) -> dict[str, object]:
    """Whether the cluster this rung ran against went through crash recovery.

    Retried, because recovery refuses connections while it runs and asking once would learn
    nothing. Every failed rung calls this, so a rung a neighbour stepped on and a rung that
    killed the server itself do not have to be told apart afterwards by reading a container
    log — which is what the first three attempts at the top of this ladder required.

    The signal is the recovery refusal, recorded verbatim, rather than anything derived. The
    obvious derived signal — the postmaster's start time moving — does not fire at all here,
    because the postmaster is precisely what survives.
    """
    attempts: list[str] = []
    after: dict[str, object] | None = None
    for attempt in range(8):
        try:
            after = await _cluster(owner)
            attempts.append("connected")
            break
        except Exception as exc:  # noqa: BLE001, PERF203 - recovery is expected here
            attempts.append(f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
            if attempt < 7:
                await asyncio.sleep(5)
    seen = " ".join([error, *attempts])
    return {
        "reachable": after is not None,
        "connection_attempts_after_the_failure": attempts,
        "the_cluster_went_through_crash_recovery": any(mark in seen for mark in _CRASHED),
        "how_that_is_known": (
            "a connection was refused with `the database system is in recovery mode`, or the "
            "client's own backend was terminated under it. Nothing but a crash produces "
            "either. The postmaster's start time is unchanged across such a crash and is "
            "therefore not the signal — it is recorded beside it only to show that."
        ),
        "postmaster_start_time_before": before,
        "postmaster_start_time_after": (after or {}).get("postmaster_start_time"),
        "cluster": after,
    }


async def _server_snapshot(owner: AsyncEngine) -> dict[str, object]:
    """`_server` on a connection of its own, for the tidy-up to call after a crash."""
    async with owner.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        snapshot = await _server(conn)
        await conn.rollback()
    return snapshot


async def _live() -> dict[str, object]:
    """The real search path, end to end, on the installation as it stands.

    This is the anchor the scratch ladder hangs from. The installation is partitioned at
    MODULUS 256 today, so this is not a projection of what 256 costs end to end — it is that
    cost, through the product's own stages, over the real corpus, with TEI embedding the
    question and the cross-encoder reranking the result.

    Its value is the denominator. `latency.json` measured 873.44 ms with 742.43 ms of it in
    the reranker, on the *unpartitioned* schema; whatever the modulus does, it does against a
    total that is 85% a model nobody here can make faster. Measured again rather than quoted
    from that file, because the schema underneath it has changed since it was written.
    """
    # Reaching into `latency.py`'s private seams on purpose. The alternative is a second
    # copy of the stage timings, and two copies of a stage boundary drift apart — which is
    # the whole reason `eval/harness.py` exists.
    from eval.harness import installation
    from eval.latency import _one, _summarise  # pyright: ignore[reportPrivateUsage]

    where = await installation()
    passes = [await _one(where, question.question) for question in where.questions[:12]]
    return {
        "measured_through": "eval.latency._one, the same seams eval/latency.json uses",
        "questions": len(passes),
        "tenant": str(where.tenant),
        **_summarise(passes),
    }


async def _verify(owner: AsyncEngine) -> dict[str, object]:
    """The corpus is as it was, and nothing of this sweep is left behind.

    `documents`, `chunks` and `chunk_embeddings` are counted because this file claims to be
    read-only with respect to them, and a claim is not a check. The `SECURITY DEFINER` count
    is here because two of the functions this sweep creates are `SECURITY DEFINER`, and a run
    that left them would leave a bypass no grep in CLAUDE.md's audit would find.
    """
    async with owner.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        left = int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM pg_namespace WHERE nspname = :s"), {"s": SCHEMA}
                )
            ).scalar_one()
        )
        counts = (
            await conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM documents) AS documents, "
                    "       (SELECT count(*) FROM chunks) AS chunks, "
                    "       (SELECT count(*) FROM chunk_embeddings) AS embeddings, "
                    "       (SELECT count(*) FROM pg_proc p JOIN pg_namespace n "
                    "        ON n.oid = p.pronamespace "
                    "        WHERE p.prosecdef AND n.nspname = 'public') AS definers, "
                    "       (SELECT version_num FROM alembic_version) AS revision"
                )
            )
        ).one()
        await conn.rollback()
    return {
        "scratch_schemas_left": left,
        "documents": int(counts.documents),
        "chunks": int(counts.chunks),
        "embeddings": int(counts.embeddings),
        "security_definers_in_public": int(counts.definers),
        "alembic_revision": str(counts.revision),
        "clean": left == 0,
    }


# --- Curves and the verdict --------------------------------------------------------------


def _curve_row(key: str, rung: dict[str, Any]) -> dict[str, object]:
    """One point on both curves, for one modulus.

    The planning term is the request's rather than one statement's: a search plans four
    statements and pays for all four. The widening term stays a ratio and is never converted
    to milliseconds — see the module docstring on what this corpus can and cannot settle.
    """
    measured = rung.get("measured") or {}
    if "unreachable" in rung:
        return {"modulus": key, "error": rung["unreachable"]}
    dense = measured.get("dense") or {}
    tsv = measured.get("tsvector") or {}
    hydrate = measured.get("hydrate") or {}
    lexical = measured.get("lexical") or {}
    pruned = lexical.get("pruned") or {}
    shipped = lexical.get("shipped") or {}
    # A rung where one statement could not be planned still measured the others, and those
    # are worth keeping — MODULUS 1024 measured a dense join and a row placement before the
    # `tsvector` statement ran out of time. The totals below are only meaningful when every
    # statement they add up is present, so they are withheld rather than silently short.
    missing = [
        name
        for name, arm in (("dense", dense), ("hydrate", hydrate), ("bm25_pruned", pruned))
        if not arm
    ]
    if not dense and not tsv and not hydrate:
        return {"modulus": key, "error": measured.get("error", "no arm completed")}

    def _total(*arms: dict[str, Any]) -> float | None:
        return None if any(not arm for arm in arms) else round(sum(map(_planning_of, arms)), 3)

    return {
        "modulus": key,
        "arms_that_did_not_complete": missing or None,
        "why": (measured.get("errors") or None) if missing else None,
        # Three totals, because a search plans four statements and one of the four is the
        # lexical half, which exists in three forms. Adding a `tsvector` lexical stage to a
        # `bm25` one would be a request no installation makes.
        #
        #   `after_stage_02`   the bm25 engine with the plpgsql fix, which is the system the
        #                      modulus is being chosen for
        #   `as_the_system_stands`  the bm25 engine as migration 0022 ships it, unpruned
        #   `tsvector_engine`  what this installation is configured with today, and what
        #                      `latency.json`'s 50.26 ms lexical stage timed
        "planning_ms_after_stage_02": _total(dense, pruned, hydrate),
        "planning_ms_as_the_system_stands": _total(dense, shipped, hydrate),
        "planning_ms_tsvector_engine": _total(dense, tsv, hydrate),
        "planning_ms_tail_after_stage_02": round(
            _tail_of(dense) + _tail_of(pruned) + _tail_of(hydrate), 3
        ),
        "planning_ms_tail_warm_after_stage_02": round(
            sum(
                float((arm.get("planning_ms") or {}).get("p95_warm") or 0.0)
                for arm in (dense, pruned, hydrate)
            ),
            3,
        ),
        "planning_ms_first_plan_on_a_connection": round(
            sum(
                float((arm.get("planning_ms") or {}).get("first") or 0.0)
                for arm in (dense, pruned, hydrate)
            ),
            3,
        ),
        "planning_by_statement": {
            "dense": _planning_of(dense),
            "tsvector": _planning_of(tsv),
            "hydrate": _planning_of(hydrate),
            "bm25_pruned": _planning_of(pruned),
            "bm25_shipped": _planning_of(shipped),
        },
        "arms_that_collapsed_to_a_generic_plan": [
            name
            for name, arm in (
                ("dense", dense),
                ("tsvector", tsv),
                ("hydrate", hydrate),
                ("bm25_pruned", pruned),
                ("bm25_shipped", shipped),
            )
            if _collapse([float(v) for v in (arm.get("planning_series_ms") or [])]) is not None
        ],
        "partitions_in_plan": {
            "dense": dense.get("partitions_in_plan"),
            "tsvector": tsv.get("partitions_in_plan"),
            "bm25_pruned": pruned.get("partitions_in_plan"),
            "bm25_shipped": shipped.get("partitions_in_plan"),
        },
        "locks_for_the_lexical_half": (lexical.get("locks_from_the_application_role") or {}),
        "execution_ms": {
            "dense": _median_of(dense, "execution_ms"),
            "tsvector": _median_of(tsv, "execution_ms"),
            "bm25_pruned": _median_of(pruned, "execution_ms"),
            "bm25_shipped": _median_of(shipped, "execution_ms"),
        },
        "dense_candidates_returned": dense.get("returned"),
        "database_half_ms": (measured.get("database_half_ms") or {}).get("median"),
        "database_half_p95_ms": (measured.get("database_half_ms") or {}).get("p95"),
        "locks_for_request": (measured.get("locks") or {}).get("for_request"),
        "widening_largest_tenant": (rung.get("widening_largest") or {}).get("widening"),
        "widening_median_tenant": (rung.get("widening_median") or {}).get("widening"),
        "share_of_corpus_reached_largest": (rung.get("widening_largest") or {}).get(
            "share_of_corpus_reached"
        ),
        "share_of_corpus_reached_median": (rung.get("widening_median") or {}).get(
            "share_of_corpus_reached"
        ),
        "tenants_sharing_the_partition": (rung.get("widening_largest") or {}).get(
            "tenants_sharing_the_partition"
        ),
    }


def _verdict(curves: dict[str, Any], grid: dict[str, Any]) -> dict[str, object]:
    """Where the total is smallest, and how flat it is around there.

    The flat region is computed rather than eyeballed, and its tolerance was declared before
    the run. If more than one rung sits inside it — which is what a flat curve means — the
    verdict says so and refuses to name a single optimum, because an optimum quoted to three
    figures out of a flat curve is worse than an honest range.
    """
    verdicts: dict[str, object] = {}
    for population, rows in curves.items():
        usable = [
            row
            for row in rows
            if "error" not in row and row.get("planning_ms_after_stage_02") is not None
        ]
        if not usable:
            verdicts[population] = {"measured": False}
            continue
        # The system the modulus is being chosen for: the dense join, the lexical half with
        # the plpgsql fix in it, and hydrate, planned and executed. Widening is deliberately
        # absent from this sum and carried as a ratio instead — see the module docstring on
        # why a millisecond of execution over 13,549 passages does not transfer.
        totals = {
            str(row["modulus"]): round(
                float(row["planning_ms_after_stage_02"])
                + float(row["execution_ms"]["dense"])
                + float(row["execution_ms"]["bm25_pruned"]),
                3,
            )
            for row in usable
        }
        best = min(totals, key=lambda key: totals[key])
        floor = totals[best]
        flat = [key for key, value in totals.items() if value <= floor * (1 + FLAT_TOLERANCE)]
        planning = {str(row["modulus"]): float(row["planning_ms_after_stage_02"]) for row in usable}
        partitioned = {key: value for key, value in planning.items() if key != "unpartitioned"}
        moved = (
            round(max(partitioned.values()) / min(partitioned.values()), 2)
            if len(partitioned) > 1 and min(partitioned.values()) > 0
            else 0.0
        )
        verdicts[population] = {
            "total_ms_by_modulus": totals,
            "minimum_at": best,
            "minimum_ms": floor,
            "flat_region_within_tolerance": flat,
            "flat_tolerance": FLAT_TOLERANCE,
            "planning_curve_moved_by": moved,
            "bar_1_curve_moved": moved >= CURVE_MOVED,
            "a_single_optimum_is_quotable": len(flat) == 1,
        }

    # Named rather than counted. A single boolean over the whole grid turns "one rung out of
    # thirteen could not plan one statement" into "the bar failed", which reads as though the
    # measurement were unsound rather than as though one rung were out of reach.
    pruning: list[dict[str, object]] = []
    interchangeable: list[str] = []
    routes_agree: list[str] = []
    empty: list[str] = []
    for population, rungs in grid.items():
        for key, rung in rungs.items():
            measured = rung.get("measured") or {}
            if "unreachable" in rung or not measured.get("dense"):
                continue
            dense = (measured.get("dense") or {}).get("subplans_removed") or []
            expected = None if key == "unpartitioned" else int(key) - 1
            lexical = measured.get("lexical") or {}
            pruning.append(
                {
                    "tenants": population,
                    "modulus": key,
                    "dense_subplans_removed": dense,
                    "dense_expected": expected,
                    "dense_as_expected": expected is None
                    or (len(dense) == 2 and all(value == expected for value in dense)),
                    "dense_pruning_was_stable": (measured.get("dense") or {}).get(
                        "pruning_was_stable"
                    ),
                    "bm25_shipped_subplans_removed": (lexical.get("shipped") or {}).get(
                        "subplans_removed"
                    ),
                    "bm25_pruned_subplans_removed": (lexical.get("pruned") or {}).get(
                        "subplans_removed"
                    ),
                    "bm25_pruned_stable": (lexical.get("pruned") or {}).get("pruning_was_stable"),
                }
            )
            if not (lexical.get("equivalence") or {}).get("interchangeable"):
                interchangeable.append(f"{population}/{key}")
            for name in ("widening_largest", "widening_median"):
                if not (rung.get(name) or {}).get("routes_agree"):
                    routes_agree.append(f"{population}/{key}/{name}")
            for arm in ("dense", "tsvector", "hydrate"):
                if not (measured.get(arm) or {}).get("returned"):
                    empty.append(f"{population}/{key}/{arm}")

    return {
        "per_population": verdicts,
        "bar_1_pruning": pruning,
        "bar_2_widening_routes_agree": not routes_agree,
        "bar_2_rungs_where_the_routes_disagreed": routes_agree,
        "bar_3_lexical_arms_interchangeable": not interchangeable,
        "bar_3_rungs_where_they_were_not": interchangeable,
        "bar_3_every_arm_returned_rows": not empty,
        "bar_3_arms_that_returned_nothing": empty,
    }


def _previous() -> dict[str, Any]:
    """Whatever an earlier run of this sweep left on disk, or nothing.

    The ladder cannot always be climbed in one process. The top of it takes the **whole
    cluster** down: the backend planning over a few thousand partitions is killed by the Linux
    OOM killer on this 7.75 GB machine and Postgres goes into crash recovery, which ends the
    run and every rung still ahead of it. Two runs died that way, and the second cost twenty
    minutes of good measurement, because the report was written only at the end.

    So a run *starts* from what is already on disk and rewrites the file after **every rung**.
    A crash then costs the rung it happened on and nothing before it, and a ladder whose top
    is not reachable in one pass can be climbed in as many passes as it takes. Shape borrowed
    from `eval/lock_budget.py`, which needed the same thing for the same reason.
    """
    if not REPORT.exists():
        return {}
    try:
        return dict(json.loads(REPORT.read_text()))
    except (OSError, ValueError):
        return {}


def _write(report: dict[str, Any], grid: dict[str, Any]) -> list[str]:
    """Recompute the curves from whatever the grid holds, and put the file on disk.

    Called after every rung, so the file is always a true statement about the rungs that have
    been measured and never a promise about the ones that have not. Returns the rungs that
    produced no measurement, which is what the exit code is built from: the very first full
    run lost twelve of fourteen rungs to a teardown bug, wrote a report saying so, and exited
    0 — which is how a partial ladder gets quoted as a whole one.
    """
    report["grid"] = grid
    flat = {pop: cell["rungs"] for pop, cell in grid.items()}
    if flat:
        report["curves"] = {
            pop: [_curve_row(key, rung) for key, rung in rungs_.items()]
            for pop, rungs_ in flat.items()
        }
        report["verdict"] = _verdict(report["curves"], flat)
    missing = [
        f"{population}/{key}"
        for population, cell in grid.items()
        for key, rung in (cell.get("rungs") or {}).items()
        if "unreachable" in rung or "error" in (rung.get("measured") or {"error": True})
    ]
    report["rungs_without_a_measurement"] = missing
    REPORT.write_text(json.dumps(report, indent=1, sort_keys=False) + "\n")
    return missing


# --- The run -----------------------------------------------------------------------------


def _engines() -> tuple[AsyncEngine, AsyncEngine]:
    """A fresh owner and application engine.

    Called again between rungs, and the recycling is the point. A backend's relation cache is
    per-connection and is never given back: after climbing 32, 64, 128, 256 and 512 the same
    two backends were holding catalogue entries for close to ten thousand relations, and the
    Linux OOM killer took one of them and with it the whole cluster, mid-ladder. Disposing the
    pool between rungs is also the more honest measurement — every rung then starts from a
    cold backend rather than inheriting the one before it.
    """
    return (
        create_async_engine(settings.database_owner_url, pool_size=2, max_overflow=0),
        create_async_engine(settings.database_url, pool_size=2, max_overflow=0),
    )


async def _run(rungs: list[int | None], populations: list[int], live: bool) -> int:
    owner, app = _engines()
    previous = _previous()
    report: dict[str, Any] = {
        "bar": {
            "1_the_curve_has_to_move": (
                f"planning at the top rung must exceed the bottom partitioned rung by "
                f"{CURVE_MOVED}x, and every partitioned rung must report Subplans Removed = "
                "P-1 on both Appends of the dense join"
            ),
            "2_the_two_routes_to_widening_must_agree": (
                "rows counted in the queried partition must equal rows predicted by "
                "reproducing Postgres's partition hash, and that hash must agree with "
                "satisfies_hash_partition, at every rung"
            ),
            "3_the_arms_must_be_the_same_query": (
                "every arm returns rows, and the shipped and pruned lexical functions return "
                "the same chunk ids at the same ranks with no NULL score (ADR 0002 F18)"
            ),
            "4_a_recommendation_needs_a_flat_region": (
                f"the report names the moduli within {FLAT_TOLERANCE:.0%} of the minimum; if "
                "more than one rung is inside it, no single optimum may be quoted"
            ),
            "set_before_the_run": True,
        },
        "rungs_requested": sorted(
            {
                *(previous.get("rungs_requested") or []),
                *("unpartitioned" if rung is None else rung for rung in rungs),
            },
            key=lambda rung: 0 if rung == "unpartitioned" else int(str(rung)),
        ),
        "populations_requested": sorted(
            {*(previous.get("populations_requested") or []), *populations}
        ),
        "attempted_in_this_pass": {
            "rungs": ["unpartitioned" if rung is None else rung for rung in rungs],
            "populations": populations,
        },
        "passes": [*(previous.get("passes") or []), time.strftime("%Y-%m-%dT%H:%M:%S+0000")],
    }
    # Carried forward from an earlier pass unless this one measures them again, so a
    # `--no-live` follow-up on one rung does not delete the end-to-end anchor.
    for carried in ("live", "widening_model", "server", "corpus"):
        if previous.get(carried) is not None:
            report[carried] = previous[carried]
    started = time.perf_counter()
    # Declared before the `try`, because the tidy-up writes it and the `try` is what may die.
    grid: dict[str, Any] = {
        population: {"assignment": cell["assignment"], "rungs": dict(cell["rungs"])}
        for population, cell in (previous.get("grid") or {}).items()
    }
    try:
        async with owner.connect() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            report["server"] = await _server(conn)
            space = (
                await conn.execute(
                    text(
                        "SELECT embedding_model AS model, embedding_version AS version, "
                        "count(*) AS n FROM chunk_embeddings GROUP BY 1, 2 "
                        "ORDER BY n DESC LIMIT 1"
                    )
                )
            ).one()
            vector = str(
                (
                    await conn.execute(
                        text(
                            "SELECT embedding_half::text FROM chunk_embeddings "
                            "WHERE embedding_model = :m AND embedding_version = :v "
                            "ORDER BY chunk_id LIMIT 1"
                        ),
                        {"m": space.model, "v": space.version},
                    )
                ).scalar_one()
            )
            corpus = (
                await conn.execute(
                    text(
                        "SELECT count(*) AS passages, count(DISTINCT tenant_id) AS tenants "
                        "FROM chunks"
                    )
                )
            ).one()
            await conn.rollback()

        report["corpus"] = {
            "passages": int(corpus.passages),
            "real_tenants": int(corpus.tenants),
            "embedding_space": {"model": str(space.model), "version": str(space.version)},
            "query_vector": (
                "a stored passage embedding rather than a question through TEI. "
                "partition-shape.json's caveat applies unchanged: the asymmetry changes which "
                "rows come back and changes nothing about planning, Subplans Removed or locks."
            ),
        }
        params: dict[str, object] = {
            "embedding": vector,
            "model": str(space.model),
            "version": str(space.version),
            "limit": WANTED,
        }
        question = "personal data processing under the regulation"
        space_pair = (str(space.model), str(space.version))

        await _drop(owner)
        for tenants in populations:
            assignment = await _assign(owner, tenants, space_pair)
            largest = str(assignment["largest"])
            median = str(assignment["median"])
            rows_total = int(str(assignment["rows"]))
            cell = grid.setdefault(str(tenants), {"assignment": assignment, "rungs": {}})
            cell["assignment"] = assignment
            per_modulus: dict[str, Any] = cell["rungs"]
            for modulus in rungs:
                key = "unpartitioned" if modulus is None else str(modulus)
                print(f"  tenants={tenants} modulus={key}", flush=True)
                try:
                    before = str((await _cluster(owner))["postmaster_start_time"])
                except Exception:  # noqa: BLE001 - a dead cluster is what the rung reports
                    before = None
                try:
                    build_s = await _build(owner, modulus, space_pair)
                    per_modulus[key] = {
                        "modulus": modulus,
                        "build_s": build_s,
                        "cluster_at_measurement": await _cluster(owner),
                        "relations_in_schema": await _relations(owner),
                        "widening_largest": await _widening(owner, modulus, largest, rows_total),
                        "widening_median": await _widening(owner, modulus, median, rows_total),
                        "measured": await _rung(owner, app, largest, params, question),
                        "measured_median_tenant": await _rung(owner, app, median, params, question),
                    }
                except Exception as exc:  # noqa: BLE001 - an unreachable rung is a result
                    await owner.dispose()
                    await app.dispose()
                    owner, app = _engines()
                    failure = f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}"
                    per_modulus[key] = {
                        "unreachable": failure,
                        "aftermath": await _survived(owner, failure, before),
                        "note": (
                            "the rung did not complete. Read `aftermath` before reading the "
                            "error: where the cluster went through crash recovery, the error "
                            "is a symptom — the backend was gone before the client heard "
                            "anything, and what it saw was the next connection attempt. Two "
                            "other measurement branches share this cluster's lock table and "
                            "its memory, and a run has already been killed from outside by a "
                            "neighbour, so re-run before concluding a rung is unreachable."
                        ),
                    }
                # Written after every rung and before the teardown that may be the thing that
                # fails. Two earlier passes died in the tidy-up and took a finished ladder
                # with them.
                _write(report, grid)
                try:
                    await _teardown(owner)
                except Exception as exc:  # noqa: BLE001 - recorded, never fatal
                    per_modulus[key]["teardown_failed"] = (
                        f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
                    )
                await app.dispose()
                await owner.dispose()
                owner, app = _engines()  # a cold backend for the next rung; see `_engines`
                _write(report, grid)

        try:
            report["widening_model"] = await _model(owner)
        except Exception as exc:  # noqa: BLE001 - the model is arithmetic, not the ladder
            report["widening_model"] = {
                "measured": False,
                "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}",
            }
        _write(report, grid)
        if live:
            try:
                report["live"] = await _live()
            except Exception as exc:  # noqa: BLE001 - a missing TEI is a result, not an abort
                report["live"] = {
                    "measured": False,
                    "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}",
                }
        _write(report, grid)
    finally:
        # Every step of the tidy-up is allowed to fail without losing the report. A rung that
        # kills the server takes the next several statements with it — MODULUS 1024 did
        # exactly that, and the run that found it died in `_drop_tables` against a cluster
        # still in crash recovery, so the finding reached the log and never reached disk.
        report["teardown"] = {}
        for name, step in (
            ("dropped", _drop(owner)),
            ("verified", _verify(owner)),
            ("server_at_end", _server_snapshot(owner)),
        ):
            try:
                report["teardown"][name] = (await step) or True
            except Exception as exc:  # noqa: BLE001, PERF203 - the report matters more
                report["teardown"][name] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
        report["verification"] = report["teardown"].get("verified")
        report["server_at_end"] = report["teardown"].get("server_at_end")
        report["took_s"] = round(time.perf_counter() - started, 1)
        report["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime())
        missing = _write(report, grid)
        await app.dispose()
        await owner.dispose()

    print(f"\nWritten to {REPORT}")
    if missing:
        print(f"{len(missing)} rung(s) produced no measurement: {', '.join(missing)}")
        return 1
    return 0


#: How `python -m eval` finds this sweep. Declared here rather than listed in `__main__.py`,
#: so adding a measurement is adding a file and nothing else.
COMMAND = "modulus-cost"
USAGE = "modulus-cost [--partitions 32,64,256] [--tenants 8,200] [--no-live]"


def cli(argv: list[str]) -> int:
    rungs: list[int | None] = list(RUNGS)
    populations = list(POPULATIONS)
    if "--partitions" in argv:
        rungs = [
            None if value.strip() in {"0", "none", "unpartitioned"} else int(value)
            for value in argv[argv.index("--partitions") + 1].split(",")
        ]
    if "--tenants" in argv:
        populations = [int(value) for value in argv[argv.index("--tenants") + 1].split(",")]
    return asyncio.run(_run(rungs, populations, "--no-live" not in argv))


def run() -> int:
    return cli(sys.argv[1:])
