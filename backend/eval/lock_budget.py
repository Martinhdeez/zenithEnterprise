# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""What `max_locks_per_transaction` has to be once `chunks` is partitioned, measured.

`eval/partition-shape.json` found the thing that breaks first, and it is not the planner. A
query over a partitioned pair takes an `AccessShareLock` on every relation of every partition
**at plan time**, before runtime pruning has removed anything, and when the shared lock table
runs out the error is `OutOfMemory` raised *during planning*. The query never runs. It is a
500 on an ordinary search, not a slow answer.

That sweep left three things open, and each is a number this one is not allowed to assume.

**1. Nine relations per partition-pair was an index count, not a lock count.** It came from
reading `\\d chunks` and `\\d chunk_embeddings` and multiplying. Whether a lock is taken per
relation, whether ParadeDB's `bm25` index is one relation or several, and whether a generated
column or a partitioned parent adds any — none of that was measured. Here the pair is built
with the installation's **actual** index set, read out of the catalogue rather than typed
into this file, and the cost per partition is the *slope* between two rungs, so the parents
and the per-transaction constants cancel instead of being estimated.

**2. `max_locks_per_transaction` is not a per-transaction cap.** It sizes one table of
`max_locks_per_transaction * (max_connections + max_prepared_transactions)` slots that the
whole cluster draws from, and `partition-shape.json` watched a single query hold 10,001 locks
against a nominal 6,400 and succeed — the hash table grows into shared memory nobody
reserved. So the ceiling is a *concurrency* limit, the surplus is other backends' to take
away, and arithmetic on the nominal number is a lower bound on safety rather than a
prediction. It is therefore verified rather than derived: the sized value is set, the server
restarted, and the same concurrency ladder run again.

**3. The prepared-statement path was measured and its result was never explained.** With
`plan_cache_mode = auto`, Postgres switched to a generic plan after six executions and
execution time went from 2.07 ms to 78.5 ms — a 37x regression at ten partitions, while the
same switch at a thousand partitions was a large win. 256 is between them, and 256 is what
stage 02 is landing. psycopg promotes a statement to a server-side prepared one after five
executions of its own accord, so the switch is on the shipping path rather than a curiosity,
and it would be on it *unpartitioned* too — which is measured here as well.

    **Corrected after the run, because this paragraph was wrong.** It said the product binds
    its query vector as text, and reasoned from there that the cast in
    `CAST(:embedding AS halfvec(1024))` is re-evaluated per row under a generic plan.
    `search.py` does pass `str(embedding)` — but the placeholder sits inside that cast, so the
    server resolves the parameter's type from its context and psycopg sends it untyped.
    `pg_prepared_statements.parameter_types` reads `{halfvec,text,text,smallint}`: the vector
    arrives as a `halfvec` and there is no per-row parsing to pay for. The reasoning was
    plausible, it was written down before the measurement, and the measurement refuted it. It
    is left here rather than deleted because the *harness* that produced the 37x — including
    `_prepared` below — declares the parameter `text` explicitly, which is why the two arms
    disagree and why the driver arm is the one to quote.

## The bar, set before the run

Stated here so the result cannot be read backwards afterwards. A failed bar stays in this
file rather than being rewritten; see `eval/coarse.py`, where one did.

**Bar 1 — sizing.** A value for `max_locks_per_transaction` counts as verified only if, at
the stage-02 modulus, `CONCURRENCY` request-shaped transactions hold their locks
simultaneously with none raising `OutOfMemory`, **and the default 64 is shown to fail the
same test on the same schema.** A pass with no paired failure is indistinguishable from a
measurement that never ran, which is the failure mode this whole directory is written
against.

**Bar 2 — prepared statements.** `plan_cache_mode = auto` may be left alone at the stage-02
modulus only if the generic plan, once adopted, executes within `GENERIC_TOLERANCE` of the
custom plan's median. Beyond that the application has to pin the mode, because the switch is
invisible: it happens on the sixth execution of a connection that has already served five
good ones, and nothing in the product reports it.

## Read-only with respect to the corpus

Everything is built inside `zenith_locks`, dropped in a `finally` and verified gone. `chunks`
and `chunk_embeddings` are read — for their index definitions, their text and their vectors —
and never written. The one arm that touches production relations directly is
`_prepared_live`, which only `PREPARE`s and `EXECUTE`s a `SELECT`.

    docker compose exec -T api python -m eval lock-budget [--partitions 64,256]
"""

from __future__ import annotations

import asyncio
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

REPORT = Path(__file__).parent / "lock-budget.json"

#: One schema, one name, dropped in a `finally`. Named for the sweep so a leftover says who
#: left it — the same discipline `eval/partition_shape.py` keeps with `zenith_partshape`.
SCHEMA = "zenith_locks"

#: The rungs. Two are enough and two are the point: the cost per partition is the *slope*
#: between them, so every per-transaction constant — the virtual transaction id, the parents,
#: the two `documents` relations — cancels rather than being guessed at. 256 is on the ladder
#: because it is what stage 02 is landing; 64 because it is a quarter of it and a partition
#: count this installation could carry today.
PARTITIONS: tuple[int, ...] = (64, 256)

#: The concurrency the sizing has to survive. This is not a wish: `api_pool_size` is 10 and
#: `worker_pool_size` is 5 with `max_overflow=0` on both, so 15 is the largest number of
#: application transactions this installation can have open at once, and the owner and
#: platform pools add two apiece. 20 is that ceiling, and the sizing section reports what a
#: larger pool would cost rather than assuming nobody will ever raise one.
CONCURRENCY = 20

#: The ladder the concurrency probe climbs. It has to bracket the failure from both sides at
#: the default setting, so it starts below where 6,400 nominal slots run out at 256
#: partitions (about two concurrent joined queries) and ends past `CONCURRENCY`.
CONCURRENCY_LADDER: tuple[int, ...] = (1, 2, 4, 8, 12, 16, 20, 24, 32)

#: How much worse a generic plan may execute before `plan_cache_mode` has to be pinned.
#: 1.5 rather than 1.0 because a generic plan that is slightly worse per execution can still
#: be the better trade once it stops paying for planning, and the planner's own switch rule
#: says exactly that. 37x is not that.
GENERIC_TOLERANCE = 1.5

#: Executions per prepared-statement arm. Eight, because Postgres plans custom-ly five times
#: before it will consider a generic plan and psycopg promotes to a server-side prepare on
#: the sixth; a run that stopped at five would report the threshold rather than what is past
#: it. Same value and same reason as `eval/partition_shape.py`.
PREPARED_RUNS = 8

#: Executions of the *driver* arm, which needs more than eight and the reason is the trap one
#: level below the one `PREPARED_RUNS` avoids. Two thresholds are in series there, not one:
#: psycopg promotes the statement to a server-side `PREPARE` on its sixth execution, and only
#: then does Postgres start counting the five custom plans it wants before it will consider a
#: generic one. Eight executions therefore produce **three** custom plans and no decision at
#: all — which the first run of this sweep reported as "the driver does not switch",
#: indistinguishable from "the driver was never asked". Sixteen clears both: five to prepare,
#: then eleven with the plan cache counting.
DRIVER_RUNS = 16

#: Timed repeats per arm. The median is what is kept: the claim under test is about a cost
#: paid once per query, which shows up in the middle of the distribution rather than at its
#: floor.
REPEATS = 7

#: Partitions created per transaction during the build. Every `CREATE TABLE ... PARTITION OF`
#: takes locks held to the end of its transaction, so an unbatched build runs out of the same
#: lock table this sweep exists to measure — and fails during setup, which looks like a result
#: and is not one.
BATCH = 64

#: Synthetic tenants that actually hold rows. Eight, as in `eval/partition_shape.py`, so the
#: queried partition holds enough vectors for the planner to still have a choice about how to
#: read it while the rest of the ladder stays empty. Nothing here depends on what a partition
#: contains: lock counts and planning costs are properties of the schema, not of the rows.
POPULATED = 8

#: What the dense half asks for. Imported rather than repeated.
WANTED = CANDIDATES


def _tenant(index: int) -> str:
    return f"00000000-0000-0000-0000-{index:012d}"


# --- Reading a plan --------------------------------------------------------------------

_PLANNING = re.compile(r"Planning Time: ([\d.]+) ms")
_EXECUTION = re.compile(r"Execution Time: ([\d.]+) ms")
_REMOVED = re.compile(r"Subplans Removed: (\d+)")


def _read_plan(lines: list[str]) -> dict[str, object]:
    """What a partitioned plan has to state about itself.

    `Subplans Removed` appears only under `ANALYZE`: pruning on a `STABLE` key happens at
    executor startup, so a plan that was never started cannot report it. Collected as a list,
    one entry per `Append`, because the join arm has two partitioned relations and a single
    number would report one relation's pruning as the query's.
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
    """Locks held by this backend right now, counted inside the transaction that took them.

    Every one of these is released at commit, so this is the only place the number exists.
    The relation locks are the ones that scale; the virtual transaction id and the rest are
    constants, and taking a *difference* against a reading from before the query is what
    removes them.
    """
    return int(
        (
            await conn.execute(text("SELECT count(*) FROM pg_locks WHERE pid = pg_backend_pid()"))
        ).scalar_one()
    )


# --- The queries, as the product writes them --------------------------------------------


def _dense(schema: str) -> str:
    """`dense()` from `app/features/retrieval/search.py`, against the scratch pair.

    The join is not a convenience and is never dropped: `chunk_embeddings` is filtered by
    tenant only, so the join to `chunks` is where label isolation is enforced for the dense
    half. A measurement without it measures a query this product must never run.

    The double `CAST(:embedding AS halfvec(1024))` is copied rather than simplified. It is
    exactly what makes the generic-plan arm below interesting: with the parameter unknown at
    plan time the cast cannot be folded, and what a custom plan does once a generic plan does
    per row.
    """
    return (
        "SELECT c.id, 1 - (e.embedding_half <=> CAST(:embedding AS halfvec(1024))) AS score "
        f"FROM {schema}.emb e "
        f"JOIN {schema}.chk c ON c.id = e.chunk_id "
        "WHERE e.embedding_model = :model AND e.embedding_version = :version "
        "ORDER BY e.embedding_half <=> CAST(:embedding AS halfvec(1024)) LIMIT :limit"
    )


def _lexical(schema: str) -> str:
    """`lexical()` from the same module: the `tsvector` half, which touches `chunks` alone."""
    return (
        f"SELECT c.id, ts_rank_cd(c.tsv, q) AS score FROM {schema}.chk c, "
        "plainto_tsquery('zenith_text', :question) q WHERE c.tsv @@ q "
        "ORDER BY score DESC LIMIT :limit"
    )


#: `dense()` from `app/features/retrieval/search.py`, verbatim, against the unpartitioned
#: tables the installation runs today. Both casts included: dropping the one in the select
#: list would halve the per-row work and measure a query this product does not run.
_LIVE_DENSE = (
    "SELECT c.id, 1 - (e.embedding_half <=> CAST(:embedding AS halfvec(1024))) AS score "
    "FROM chunk_embeddings e JOIN chunks c ON c.id = e.chunk_id "
    "WHERE e.embedding_model = :model AND e.embedding_version = :version "
    "ORDER BY e.embedding_half <=> CAST(:embedding AS halfvec(1024)) LIMIT :limit"
)


def _hydrate(schema: str) -> str:
    """`hydrate()`: the third statement of a search request, and the third visit to `chunks`."""
    return f"SELECT c.id, c.text FROM {schema}.chk c WHERE c.id = ANY(:ids)"


async def _context(conn: AsyncConnection, tenant: str, labels: str = "") -> None:
    """The session the application would have, set the way `set_rls_context` sets it.

    Identical but for the timeout, which is raised well above the product's 10 s: planning
    over a few thousand partitions can exceed it, and a rung that timed out would be reported
    as a failure to plan rather than as the planning cost it is.

    `labels` is empty for the scratch schema, whose rows are all unlabelled, and is the
    tenant's whole label set for the arms that run against `public`. The first version of
    this sweep left it empty everywhere and the live arm returned **nothing**: the policy on
    `chunks` admits an unlabelled row or a row overlapping the caller's labels, and this
    installation's chunks are labelled. Three plans were then compared against each other for
    an empty result set and all three agreed, which is what a null result looks like when
    nobody checks the row count. `returned` is in the report for that reason.
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
    # A parallel plan over thousands of partitions asks for a shared memory segment larger
    # than this container has and fails with `DiskFull`, which names the wrong resource
    # entirely. Serial here, as in `eval/partition_shape.py`, and for the same reason.
    await conn.execute(text("SET LOCAL max_parallel_workers_per_gather = 0"))


# --- What a partition actually costs ----------------------------------------------------


async def _production_relations(conn: AsyncConnection) -> dict[str, object]:
    """The installation's index set on the two tables, read out of the catalogue.

    `partition-shape.json` projected its lock counts with a hand-written "9 relations per
    partition-pair", taken from an index count at the time it was written. That is exactly
    the kind of number CLAUDE.md says rots: stage 02 changes the schema, and a constant in a
    docstring does not change with it. So it is read, with the definitions, and the sweep
    replays those definitions onto the scratch pair rather than paraphrasing them.
    """
    rows = (
        await conn.execute(
            text(
                "SELECT c.relname AS table_name, i.relname AS index_name, "
                "       pg_get_indexdef(x.indexrelid) AS definition, am.amname AS method "
                "FROM pg_index x "
                "JOIN pg_class c ON c.oid = x.indrelid "
                "JOIN pg_class i ON i.oid = x.indexrelid "
                "JOIN pg_am am ON am.oid = i.relam "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' "
                "AND c.relname IN ('chunks', 'chunk_embeddings') ORDER BY 1, 2"
            )
        )
    ).all()
    by_table: dict[str, list[dict[str, str]]] = {"chunks": [], "chunk_embeddings": []}
    for row in rows:
        by_table[str(row.table_name)].append(
            {
                "index": str(row.index_name),
                "method": str(row.method),
                "definition": str(row.definition),
            }
        )
    # One relation for the heap plus one per index, which is the hypothesis this sweep
    # actually tests: the measured slope below either agrees with this number or it does not,
    # and the disagreement would be the finding.
    per_table = {name: 1 + len(indexes) for name, indexes in by_table.items()}
    return {
        "indexes": by_table,
        "relations_per_table": per_table,
        "relations_per_partition_pair_expected": sum(per_table.values()),
        "note": (
            "Read from the running installation's catalogue, not written down here. The "
            "measured slope in `rungs` is what the lock table is actually charged; this is "
            "the schema's claim about what that should be."
        ),
    }


# --- Building the scratch pair ----------------------------------------------------------

_PARENT_DDL = """
CREATE TABLE {schema}.emb (
  chunk_id          uuid NOT NULL,
  tenant_id         uuid NOT NULL,
  embedding_model   varchar NOT NULL,
  embedding_version varchar NOT NULL,
  embedding_half    halfvec(1024) NOT NULL
) PARTITION BY HASH (tenant_id);
CREATE TABLE {schema}.chk (
  id           uuid NOT NULL,
  document_id  uuid NOT NULL,
  tenant_id    uuid NOT NULL,
  label_ids    uuid[] NOT NULL DEFAULT '{{}}',
  text         varchar NOT NULL,
  tsv          tsvector GENERATED ALWAYS AS
                 (to_tsvector('zenith_text'::regconfig, text::text)) STORED,
  unlabelled   boolean GENERATED ALWAYS AS (label_ids = '{{}}'::uuid[]) STORED
) PARTITION BY HASH (tenant_id);
"""

#: The installation's indexes, replayed on the parents so Postgres propagates one copy to
#: every partition. Written out rather than generated from `pg_get_indexdef` because two of
#: them cannot survive the translation unchanged, and both differences are findings stage 02
#: has to carry:
#:
#:   * **A unique constraint on a partitioned table must include every partitioning column.**
#:     `pk_chunks` is `(id)` and `pk_chunk_embeddings` is
#:     `(chunk_id, embedding_model, embedding_version)`; under `PARTITION BY HASH (tenant_id)`
#:     both are rejected outright and have to gain `tenant_id`. That widens the primary key of
#:     both tables and it is not optional.
#:   * The bm25 index carries a Spanish stemmer configured in migration 0024. It is repeated
#:     here in the short form, because what this sweep measures about it is that it is *one
#:     relation* and therefore one lock — which the probe confirmed and which was the open
#:     question, ParadeDB having shipped versions where a bm25 index was a schema of tables.
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


async def _drop(owner: AsyncEngine) -> None:
    """Drop the scratch schema in batches, because `CASCADE` on thousands of tables cannot.

    `DROP SCHEMA ... CASCADE` is one transaction taking an `AccessExclusiveLock` on every
    object in it — more locks than the shared table holds at the top of this ladder, so the
    tidy-up fails and the schema survives. That is the one outcome this file may not produce.
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


async def _build(owner: AsyncEngine, count: int, space: tuple[str, str]) -> float:
    """Everything for one rung, in autocommit so no transaction accumulates the lock table.

    Every partition gets `ENABLE ROW LEVEL SECURITY` and a policy of its own, in the same
    statement batch as the `CREATE TABLE`. **A partition inherits neither.** CLAUDE.md's first
    invariant records it and `eval/partition-rls.sql` demonstrates it against this database:
    the parent answers correctly while a partition addressed directly hands back another
    tenant in full. `app.core.partitions.partition_statements` is the shape being copied here
    — copied rather than imported, because that helper writes `FOR VALUES IN` for the
    list-per-tenant model and this schema is `HASH`.

    It is not decoration for a lock count either. A policy on a partition is an expression the
    planner has to fetch and apply for every unpruned child, so a ladder built without them
    would understate planning at exactly the rung where planning is the thing being watched.
    """
    started = time.perf_counter()
    async with owner.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        for statement in _PARENT_DDL.format(schema=SCHEMA).split(";"):
            if statement.strip():
                await conn.execute(text(statement))
        for template in _INDEX_DDL + _POLICY_DDL:
            await conn.execute(text(template.format(schema=SCHEMA)))
        emb_policy = TENANT_POLICY.replace("'", "''")
        chk_policy = LABEL_POLICY.replace("'", "''")
        for low in range(0, count, BATCH):
            high = min(low + BATCH, count)
            await conn.execute(
                text(
                    "DO $do$ DECLARE i int; BEGIN "
                    f"FOR i IN {low}..{high - 1} LOOP "
                    f"  EXECUTE 'CREATE TABLE {SCHEMA}.e' || i "
                    f"       || ' PARTITION OF {SCHEMA}.emb "
                    f"FOR VALUES WITH (MODULUS {count}, REMAINDER ' || i || ')'; "
                    f"  EXECUTE 'ALTER TABLE {SCHEMA}.e' || i "
                    "       || ' ENABLE ROW LEVEL SECURITY'; "
                    f"  EXECUTE 'CREATE POLICY e' || i || '_isolation ON {SCHEMA}.e' || i "
                    f"       || ' USING ({emb_policy}) WITH CHECK ({emb_policy})'; "
                    f"  EXECUTE 'CREATE TABLE {SCHEMA}.c' || i "
                    f"       || ' PARTITION OF {SCHEMA}.chk "
                    f"FOR VALUES WITH (MODULUS {count}, REMAINDER ' || i || ')'; "
                    f"  EXECUTE 'ALTER TABLE {SCHEMA}.c' || i "
                    "       || ' ENABLE ROW LEVEL SECURITY'; "
                    f"  EXECUTE 'CREATE POLICY c' || i || '_isolation ON {SCHEMA}.c' || i "
                    f"       || ' USING ({chk_policy}) WITH CHECK ({chk_policy})'; "
                    "END LOOP; END $do$;"
                )
            )
        await _load(conn, space)
    return round(time.perf_counter() - started, 1)


async def _assign(conn: AsyncConnection, space: tuple[str, str]) -> int:
    """One table saying which synthetic tenant each real chunk belongs to.

    Written once and reused by every rung, so the rows in a given tenant are the same rows at
    64 partitions and at 256 and the comparison is of partition counts and nothing else.
    Assignment is by `md5(chunk_id)`, not by document, so no tenant's vectors sit
    unrepresentatively together in the graph.
    """
    await conn.execute(
        text(
            f"CREATE TABLE {SCHEMA}.assign AS SELECT chunk_id, "
            "('00000000-0000-0000-0000-' || lpad((("
            "  ('x' || substr(md5(chunk_id::text), 1, 8))::bit(32)::bigint & 2147483647"
            f") % {POPULATED})::text, 12, '0'))::uuid AS tenant_id "
            "FROM chunk_embeddings "
            "WHERE embedding_model = :model AND embedding_version = :version"
        ),
        {"model": space[0], "version": space[1]},
    )
    await conn.execute(text(f"ALTER TABLE {SCHEMA}.assign ADD PRIMARY KEY (chunk_id)"))
    return int((await conn.execute(text(f"SELECT count(*) FROM {SCHEMA}.assign"))).scalar_one())


async def _load(conn: AsyncConnection, space: tuple[str, str]) -> None:
    """Route the corpus into whichever partitions hold it, and analyse only those.

    `ANALYZE` on the parent would touch every partition in one transaction and run out of the
    lock table — the same failure `_drop` avoids, from the other direction. `pg_relation_size
    > 0` rather than `reltuples`, which is -1 on a never-analysed table: that is every
    partition here, so the obvious filter selects all of them.
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
            "  AND pg_relation_size(c.oid) > 0 LOOP "
            "  EXECUTE 'ANALYZE ' || r.rel; END LOOP; END $do$;"
        )
    )


async def _teardown(owner: AsyncEngine) -> None:
    """Drop the rung's pair, keeping `assign` so the next rung loads the same rows."""
    async with owner.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        while True:
            names = [
                str(row[0])
                for row in await conn.execute(
                    text(
                        "SELECT c.relname FROM pg_class c JOIN pg_namespace n "
                        "ON n.oid = c.relnamespace WHERE n.nspname = :s AND c.relkind = 'r' "
                        "AND c.relname <> 'assign' LIMIT :n"
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


async def _relations(conn: AsyncConnection) -> int:
    """Relations in the scratch schema, tables and indexes alike, less `assign` and its key.

    The catalogue's own count, so that "locks per partition" can be checked against
    "relations per partition" rather than assumed equal to it.
    """
    return int(
        (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid = c.relnamespace WHERE n.nspname = :s "
                    "AND c.relkind IN ('r', 'i') AND c.relname NOT LIKE 'assign%'"
                ),
                {"s": SCHEMA},
            )
        ).scalar_one()
    )


# --- One rung --------------------------------------------------------------------------


async def _rung(app: AsyncEngine, params: dict[str, object], tenant: str) -> dict[str, object]:
    """The whole of a search request in one transaction, with the locks counted per statement.

    Counted per statement because the interesting claim is that they do not add up. A lock is
    taken per *relation*, and the planner opens every index of every relation it plans
    regardless of which one it eventually uses — so the second and third statements of a
    request, which visit `chunks` again, should take no new locks at all. If they do, the
    per-request budget is larger than the per-query one and every number downstream of this
    is wrong.

    `tenant_session` is one transaction for the whole request and locks are released at
    commit, so the union over its statements is what the shared table is charged.
    """
    async with app.connect() as conn:
        try:
            await _context(conn, tenant)
            baseline = await _locks(conn)

            plan = _read_plan(await _explain(conn, _dense(SCHEMA), params))
            after_dense = await _locks(conn)

            await conn.execute(
                text(_lexical(SCHEMA)), {"question": "plazo de detencion", "limit": WANTED}
            )
            after_lexical = await _locks(conn)

            ids = [
                row[0]
                for row in await conn.execute(text(f"SELECT id FROM {SCHEMA}.chk LIMIT {WANTED}"))
            ]
            await conn.execute(text(_hydrate(SCHEMA)), {"ids": ids})
            after_hydrate = await _locks(conn)

            times: list[float] = []
            returned = 0
            for _ in range(REPEATS):
                started = time.perf_counter()
                result = await conn.execute(text(_dense(SCHEMA)), params)
                returned = len(result.all())
                times.append((time.perf_counter() - started) * 1000)
            ordered = sorted(times)
            return {
                **plan,
                "locks_baseline": baseline,
                "locks_after_dense": after_dense,
                "locks_after_lexical": after_lexical,
                "locks_after_hydrate": after_hydrate,
                "locks_for_dense": after_dense - baseline,
                "locks_added_by_lexical": after_lexical - after_dense,
                "locks_added_by_hydrate": after_hydrate - after_lexical,
                "locks_for_request": after_hydrate - baseline,
                "returned": returned,
                "median_ms": round(statistics.median(times), 2),
                "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 2),
                "min_ms": round(min(times), 2),
            }
        except Exception as exc:  # noqa: BLE001 - a failure here is a result, not an abort
            return {"error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"}
        finally:
            await conn.rollback()


# --- The concurrency probe ---------------------------------------------------------------


class _Gate:
    """Release every worker only once all of them have finished taking their locks.

    A timer would not do. The probe is only a probe if N transactions hold their locks *at
    the same time*, and the first worker committing before the last one has planned turns the
    whole ladder into one transaction measured N times — which would pass every rung and mean
    nothing. Failures count as arrivals too: a worker that ran out of the lock table is
    finished with it, and waiting for it to succeed would deadlock the rung it just proved.
    """

    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.arrived = 0
        self.event = asyncio.Event()

    def arrive(self) -> None:
        self.arrived += 1
        if self.arrived >= self.expected:
            self.event.set()


async def _hold(
    app: AsyncEngine, params: dict[str, object], tenant: str, gate: _Gate
) -> dict[str, object]:
    """Plan the query, hold the locks, and wait to be let go.

    Planning is what takes the locks — `partition-shape.json` established that the failure is
    raised *during* planning — so `EXPLAIN` without `ANALYZE` is enough to charge the shared
    table, and it keeps the probe from also measuring HNSW.
    """
    async with app.connect() as conn:
        try:
            await _context(conn, tenant)
            before = await _locks(conn)
            await conn.execute(text(f"EXPLAIN (COSTS OFF) {_dense(SCHEMA)}"), params)
            held = await _locks(conn) - before
            gate.arrive()
            await gate.event.wait()
            return {"ok": True, "locks": held}
        except Exception as exc:  # noqa: BLE001 - the failure *is* the measurement here
            gate.arrive()
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"}
        finally:
            await conn.rollback()


async def _concurrency(
    url: str, params: dict[str, object], tenant: str, ladder: tuple[int, ...]
) -> dict[str, object]:
    """How many of those transactions fit at once, bracketed from both sides.

    A bracket rather than a threshold, and the reason is in `partition-shape.json`: the lock
    hash table grows into shared memory nobody reserved, so where it stops depends on what
    every other backend happened to want at that moment. The highest rung that passed and the
    lowest that failed is what was observed; a single number would be a claim this
    measurement cannot support.

    Each rung gets its own engine so a poisoned connection cannot be handed to the next one.
    """
    rungs: list[dict[str, object]] = []
    clean: list[int] = []
    broke: list[int] = []
    for concurrent in ladder:
        engine = create_async_engine(url, pool_size=concurrent, max_overflow=0)
        gate = _Gate(concurrent)
        try:
            results = await asyncio.gather(
                *(_hold(engine, params, tenant, gate) for _ in range(concurrent))
            )
        finally:
            await engine.dispose()

        failed = [str(r.get("error")) for r in results if not r.get("ok")]
        succeeded = [int(str(r["locks"])) for r in results if r.get("ok")]
        (clean if not failed else broke).append(concurrent)
        rungs.append(
            {
                "concurrent": concurrent,
                "succeeded": len(succeeded),
                "failed": len(failed),
                "locks_each": succeeded[0] if succeeded else None,
                "locks_total_charged": sum(succeeded),
                "first_error": failed[0] if failed else None,
            }
        )
        print(
            f"    {concurrent:>3} concurrent  {len(succeeded)} ok, {len(failed)} failed"
            + (f"  ({failed[0]})" if failed else ""),
            flush=True,
        )

    return {
        "ladder": rungs,
        "highest_clean": max(clean, default=None),
        "lowest_failure": min(broke, default=None),
        "note": (
            "A bracket, not a threshold. The lock hash table grows into unreserved shared "
            "memory, so the boundary moves with whatever else the cluster is doing; the "
            "highest rung seen clean is the number to plan against and the lowest seen to "
            "fail is the one that must never be reachable."
        ),
    }


# --- The prepared-statement path ----------------------------------------------------------


async def _prepared(
    app: AsyncEngine,
    statement: str,
    params: dict[str, object],
    tenant: str,
    label: str,
    labels: str = "",
) -> dict[str, object]:
    """Custom and generic plans for the same statement, and what `auto` chooses between them.

    `auto` is measured beside the two forced modes because it is the one that ships. Postgres
    plans a prepared statement custom-ly five times and then compares: if the generic plan's
    estimate is no worse than the average custom cost plus the planning it would save, it
    switches. The planning-time column shows the switch happening; the execution-time column
    shows what it cost.

    **This arm is a proxy and it overstates.** `PREPARE ... (text)` declares the parameter as
    `text`, which is what `eval/partition_shape.py` did and where its 37x came from: with the
    parameter typed `text` the cast to `halfvec(1024)` is genuine work and a generic plan pays
    it per row. The driver does not do that — it sends the parameter untyped and the server
    infers `halfvec` from the cast context — so this arm answers "what does a generic plan
    cost if the parameter really is text", and `_driver` answers "what does the product do".
    Both are reported; only the second is the product.

    The vector is inlined into the `EXECUTE` rather than bound, so the only parameter in play
    is the one `PREPARE` declared — which is the one whose plan-time absence is under test.
    The name carries the arm's label because a `DEALLOCATE` that never ran leaves the next
    arm reporting `DuplicatePreparedStatement` instead of a measurement, which is how
    `partition-shape.json`'s top two rungs lost their forced modes.
    """
    out: dict[str, object] = {}
    for mode in ("auto", "force_custom_plan", "force_generic_plan"):
        name = f"lockbudget_{label}_{mode}"
        async with app.connect() as conn:
            try:
                await _context(conn, tenant, labels)
                await conn.execute(text(f"SET LOCAL plan_cache_mode = {mode}"))
                prepared = (
                    statement.replace(":embedding", "$1")
                    .replace(":model", f"'{params['model']}'")
                    .replace(":version", f"'{params['version']}'")
                    .replace(":limit", str(params["limit"]))
                )
                await conn.execute(text(f"PREPARE {name} (text) AS {prepared}"))
                runs: list[dict[str, object]] = []
                for _ in range(PREPARED_RUNS):
                    lines = await _explain(conn, f"EXECUTE {name}('{params['embedding']}')", {})
                    runs.append(_read_plan(lines))
                # The rows, not only the plan. Three modes agreeing on an empty result set is
                # not three modes agreeing; `_context` records what that cost the first time.
                returned = len(
                    (await conn.execute(text(f"EXECUTE {name}('{params['embedding']}')"))).all()
                )
                await conn.execute(text(f"DEALLOCATE {name}"))
                executions = [float(str(r["execution_ms"])) for r in runs if r["execution_ms"]]
                plannings = [
                    float(str(r["planning_ms"])) for r in runs if r["planning_ms"] is not None
                ]
                # The last three, not all eight: the switch happens on the sixth, so an
                # average over all eight is an average of two different plans and describes
                # neither. `settled_*` is what the connection does from then on, which is
                # what a long-lived pooled connection actually pays.
                out[mode] = {
                    "planning_ms": [r["planning_ms"] for r in runs],
                    "execution_ms": [r["execution_ms"] for r in runs],
                    "subplans_removed": [r["subplans_removed"] for r in runs],
                    "index_ordered": [r["index_ordered"] for r in runs],
                    "settled_planning_ms": round(statistics.median(plannings[-3:]), 3)
                    if plannings
                    else None,
                    "settled_execution_ms": round(statistics.median(executions[-3:]), 3)
                    if executions
                    else None,
                    "leaf_scans": runs[-1]["leaf_scans"],
                    "returned": returned,
                }
            except Exception as exc:  # noqa: BLE001 - same reason as `_rung`
                out[mode] = {"error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"}
            finally:
                await conn.rollback()
    return out


def _verdict(arms: dict[str, object]) -> dict[str, object]:
    """Bar 2, applied to one set of arms, with the ratio it was applied to."""
    auto = arms.get("auto")
    custom = arms.get("force_custom_plan")
    generic = arms.get("force_generic_plan")
    if not (isinstance(auto, dict) and isinstance(custom, dict) and isinstance(generic, dict)):
        return {"measured": False, "why": "an arm did not run"}
    settled_auto = auto.get("settled_execution_ms")
    settled_custom = custom.get("settled_execution_ms")
    settled_generic = generic.get("settled_execution_ms")
    if not (settled_auto and settled_custom and settled_generic):
        return {"measured": False, "why": "an arm produced no execution time"}
    rows = [arm.get("returned") for arm in (auto, custom, generic)]
    if not all(isinstance(count, int) and count > 0 for count in rows):
        # Not a pass and not a fail. Three plans compared over an empty result set agree
        # because there is nothing for them to disagree about, and reporting that as a pass
        # is the exact failure this directory keeps recording: a null result and an
        # experiment that never ran are indistinguishable unless the report says which.
        return {"measured": False, "why": f"an arm returned no rows: {rows}", "returned": rows}
    # `auto` against `force_custom_plan`, because `auto` is what ships and the custom plan is
    # what it starts out doing. The generic ratio is reported beside it so a reader can see
    # whether `auto` switched at all: if it did not, the two ratios differ.
    ratio_auto = round(float(settled_auto) / float(settled_custom), 2)
    ratio_generic = round(float(settled_generic) / float(settled_custom), 2)
    switched = ratio_auto > 1.2 and abs(ratio_auto - ratio_generic) < max(1.0, ratio_generic * 0.3)
    return {
        "measured": True,
        "settled_custom_ms": settled_custom,
        "settled_auto_ms": settled_auto,
        "settled_generic_ms": settled_generic,
        "auto_over_custom": ratio_auto,
        "generic_over_custom": ratio_generic,
        "auto_appears_to_have_switched": switched,
        "tolerance": GENERIC_TOLERANCE,
        "passes_bar_2": ratio_auto <= GENERIC_TOLERANCE,
    }


async def _prepared_live(
    app: AsyncEngine, params: dict[str, object], tenant: str, labels: str
) -> dict[str, object]:
    """The same three arms against the *unpartitioned* installation, read-only.

    Here because the switch is not a partitioning problem that arrives with stage 02. The
    product binds its query vector as text and casts it inside the statement, psycopg
    server-prepares after five executions, and `plan_cache_mode` is `auto` today — so if a
    generic plan is bad on this schema, it is bad now, on a connection that has already served
    five good searches.
    """
    return await _prepared(app, _LIVE_DENSE, params, tenant, "live", labels)


async def _driver(
    app: AsyncEngine,
    statement: str,
    params: dict[str, object],
    tenant: str,
    labels: str,
) -> dict[str, object]:
    """Does the driver prepare, and what does the switch cost through it? Asked, not assumed.

    Everything above measures `plan_cache_mode` through an explicit `PREPARE`/`EXECUTE`, which
    is a proxy. This is the thing itself: `dense()`'s statement, its parameters bound the way
    `search.py` binds them, run eight times on one pooled connection of the application
    engine, timed. psycopg promotes a statement to a server-side `PREPARE` after
    `prepare_threshold` executions — 5 by default — and nothing in `app/core/database.py` sets
    that threshold either way, so what happens here is what happens to the sixth search on a
    connection that has already served five.

    `pg_prepared_statements` answers the first half, because it is the catalogue's answer
    rather than the driver's documentation. The timings answer the second, and they are the
    ones worth quoting: a proxy that agrees with the product is evidence, and a proxy quoted
    *instead of* the product is the number that rots.

    The statement is `dense()`'s verbatim, both casts included. Dropping the one in the select
    list would halve the per-row work and measure a query this product does not run.

    **`generic_plans` and `custom_plans` settle it rather than the timings.** Postgres 17 keeps
    those counters per prepared statement in `pg_prepared_statements`, so whether the switch
    happened is a fact to read instead of a slowdown to infer. It matters because the driver's
    prepared statement is *not* the one an explicit `PREPARE` above creates: psycopg
    parameterises all four binds, including `LIMIT`, and a generic plan for an unknown `LIMIT`
    is estimated differently from one whose limit is a literal. That difference is enough to
    change what `auto` decides, which is why the proxy above cannot be quoted for the product.
    """
    async with app.connect() as conn:
        await _context(conn, tenant, labels)
        seen: list[int] = []
        timings: list[float] = []
        returned = 0
        for _ in range(DRIVER_RUNS):
            began = time.perf_counter()
            returned = len((await conn.execute(text(statement), params)).all())
            timings.append(round((time.perf_counter() - began) * 1000, 2))
            seen.append(
                int(
                    (
                        await conn.execute(text("SELECT count(*) FROM pg_prepared_statements"))
                    ).scalar_one()
                )
            )
        cached = [
            {
                "name": str(row.name),
                "statement": str(row.statement)[:60],
                "parameter_types": str(row.parameter_types),
                "generic_plans": int(row.generic_plans),
                "custom_plans": int(row.custom_plans),
            }
            for row in await conn.execute(
                text(
                    "SELECT name, statement, parameter_types::text AS parameter_types, "
                    "generic_plans, custom_plans FROM pg_prepared_statements ORDER BY name"
                )
            )
        ]
        await conn.rollback()
    prepared_at = next((index for index, count in enumerate(seen) if count > 0), None)
    # The instrument's own statement is prepared too, and it *does* take a generic plan —
    # `SELECT count(*) FROM pg_prepared_statements` has no parameters, so there is nothing for
    # a custom plan to specialise on. Counting it would answer the question with the counter
    # that is not being asked about, so the arm's verdict comes from the statement under test
    # alone, identified by its text rather than by its position.
    subject = [entry for entry in cached if "halfvec" in str(entry["statement"])]
    generic = sum(int(str(entry["generic_plans"])) for entry in subject)
    custom = sum(int(str(entry["custom_plans"])) for entry in subject)
    # Five and five, from the two ends. The switch, if it comes, comes late — psycopg has to
    # prepare first and Postgres has to build five custom plans after that — so a median over
    # the whole run would average the two regimes and describe neither.
    before, after = timings[:5], timings[-5:]
    return {
        "rows_returned_per_execution": returned,
        "executions": DRIVER_RUNS,
        "prepared_statements_after_each_execution": seen,
        "cached_statements": cached,
        "generic_plans_for_the_statement_under_test": generic,
        "custom_plans_for_the_statement_under_test": custom,
        "chose_a_generic_plan": generic > 0,
        "reached_the_generic_decision": custom >= 5 or generic > 0,
        "server_prepares": bool(seen and seen[-1] > 0),
        "first_execution_that_prepared": prepared_at + 1 if prepared_at is not None else None,
        "round_trip_ms": timings,
        "median_first_five_ms": round(statistics.median(before), 2) if before else None,
        "median_last_five_ms": round(statistics.median(after), 2) if after else None,
        "ratio_last_over_first": (
            round(statistics.median(after) / statistics.median(before), 2)
            if before and after
            else None
        ),
        "note": (
            "Timed round trips through the application engine, so this is the product's own "
            "path rather than a PREPARE standing in for it. `round_trip_ms` includes the "
            "network and the driver. `chose_a_generic_plan` is Postgres 17's own counter and "
            "is the fact; the timings are the consequence and are quoted second. "
            "`reached_the_generic_decision` false means the run ended before Postgres had "
            "built the five custom plans it wants first, and the arm decided nothing."
        ),
    }


# --- The server ----------------------------------------------------------------------------


async def _server(conn: AsyncConnection) -> dict[str, object]:
    """The settings that bound this sweep, and the shared memory they cost, from the server.

    `shared_memory_size` is a read-only GUC computed at startup from the settings that size
    the segment, so comparing it across two restarts is a *measurement* of what raising
    `max_locks_per_transaction` costs rather than an estimate from a per-slot constant nobody
    has checked against this build.
    """
    rows = (
        await conn.execute(
            text(
                "SELECT name, setting, unit FROM pg_settings WHERE name IN ("
                "'max_locks_per_transaction', 'max_connections', 'max_prepared_transactions', "
                "'max_pred_locks_per_transaction', 'shared_buffers', 'work_mem', "
                "'plan_cache_mode', 'enable_partition_pruning', 'enable_partitionwise_join', "
                "'shared_memory_size', 'server_version')"
            )
        )
    ).all()
    values = {str(row.name): str(row.setting) for row in rows}
    units = {str(row.name): str(row.unit) for row in rows if row.unit}
    slots = int(values["max_locks_per_transaction"]) * (
        int(values["max_connections"]) + int(values["max_prepared_transactions"])
    )
    allocations = {
        str(row[0]): int(row[1])
        for row in await conn.execute(
            text(
                "SELECT name, size FROM pg_shmem_allocations "
                "WHERE name IN ('LOCK hash', 'PROCLOCK hash', "
                "'Fast Path Strong Relation Lock Data', '<anonymous>')"
            )
        )
    }
    return {
        **values,
        "units": units,
        "nominal_lock_slots": slots,
        "shmem_allocations_bytes": allocations,
        "note": (
            "max_locks_per_transaction * (max_connections + max_prepared_transactions) sizes "
            "one table the whole cluster draws from; it is not a per-transaction allowance. "
            "`LOCK hash` in pg_shmem_allocations is the hash header only — the entries are "
            "allocated out of the anonymous pool, which is why shared_memory_size is the "
            "figure that moves and the one quoted as the cost."
        ),
    }


# --- Sizing --------------------------------------------------------------------------------


def _sizing(
    demand: int,
    relations: int,
    partitions: int,
    concurrency: int,
    max_connections: int,
    prepared: int,
) -> dict[str, object]:
    """What the setting has to be, at two ways of bounding load.

    `demand` is the lock count a request-shaped transaction was **measured** to take, not
    `relations * partitions`. The two differ by the parents — two partitioned tables and their
    seven partitioned indexes, nine locks that the product of the slope and the partition
    count does not contain — and quoting the product would put a number in this report that
    the `rungs` beside it contradict. Small, and the sort of small that a reader is right to
    stop trusting the rest of a report over.

    Both figures are the same inequality — `mlpt * (max_connections +
    max_prepared_transactions)` must exceed `demand * C` — differing only in what C is allowed
    to be, and that choice is a judgement about the deployment rather than a measurement:

    * **pool-bounded** takes C from the connection pools as configured, which is the number of
      application transactions this installation can actually have open. It is the cheapest
      and it stops being true the moment somebody raises `api_pool_size`.
    * **connection-bounded** takes C = max_connections, so the setting holds whatever the
      pools are later set to. The `max_connections` term then cancels and it collapses to
      `demand` — the tidiest form of the answer, and the dearest.
    """
    pool_bounded = -(-demand * concurrency // (max_connections + prepared))
    return {
        "relations_per_partition_pair_measured": relations,
        "partitions": partitions,
        "locks_per_request_measured": demand,
        "locks_per_request_from_the_slope_alone": relations * partitions,
        "max_connections": max_connections,
        "max_prepared_transactions": prepared,
        "pool_bounded": {
            "concurrency": concurrency,
            "max_locks_per_transaction": pool_bounded,
            "derivation": (
                "ceil(relations * partitions * concurrency / "
                "(max_connections + max_prepared_transactions))"
            ),
            "holds_while": (
                "api_pool_size + worker_pool_size + the owner and platform pools stay at or "
                "below the concurrency above. Raising a pool invalidates it silently."
            ),
        },
        "connection_bounded": {
            "concurrency": max_connections,
            "max_locks_per_transaction": demand,
            "derivation": (
                "the measured locks per request. The max_connections term cancels: every "
                "backend may be running this query, and the slot table is sized per "
                "connection, so a value covering one backend covers all of them."
            ),
            "holds_while": "the partition count and the index set do not grow.",
        },
    }


# --- The sweep -------------------------------------------------------------------------------


async def _cluster(conn: AsyncConnection) -> dict[str, object]:
    """Who else was on this database while the concurrency probe ran.

    The lock table is shared by the whole cluster, so a bracket taken beside another sweep is
    a bracket for two sweeps. That has to be recorded rather than hoped away: this
    installation is worked on from several worktrees at once — `eval/partition_shape.py` says
    so in its own comments — and a leftover `zenith_*` schema belonging to somebody else is
    the visible half of a run that may or may not be between phases.

    `locks_held_by_others` is the number that actually invalidates a reading. It is taken
    before the probe starts, so a rung that failed with a large value there failed for a
    reason this sweep does not own.
    """
    schemas = [
        str(row[0])
        for row in await conn.execute(
            text(
                "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'zenith%' "
                "AND nspname <> :mine ORDER BY 1"
            ),
            {"mine": SCHEMA},
        )
    ]
    row = (
        await conn.execute(
            text(
                "SELECT (SELECT count(*) FROM pg_stat_activity "
                "        WHERE datname = current_database() "
                "        AND pid <> pg_backend_pid()) AS backends, "
                "       (SELECT count(*) FROM pg_stat_activity "
                "        WHERE datname = current_database() AND state = 'active' "
                "        AND pid <> pg_backend_pid()) AS active, "
                "       (SELECT count(*) FROM pg_locks WHERE pid <> pg_backend_pid()) AS locks"
            )
        )
    ).one()
    return {
        "other_scratch_schemas": schemas,
        "other_backends": int(row.backends),
        "other_backends_active": int(row.active),
        "locks_held_by_others": int(row.locks),
        "note": (
            "The lock table is one table for the whole cluster. Locks held by anything else "
            "come out of the same slots this sweep is measuring, so a non-trivial "
            "`locks_held_by_others` or a foreign scratch schema means the bracket below is a "
            "bracket for this database as it was, not for this sweep alone."
        ),
    }


async def _verify(owner: AsyncEngine) -> dict[str, object]:
    """The corpus is untouched and the scratch schema is gone. Both asked, neither assumed."""
    async with owner.connect() as conn:
        left = int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM pg_namespace WHERE nspname = :s"), {"s": SCHEMA}
                )
            ).scalar_one()
        )
        row = (
            await conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM documents) AS documents, "
                    "(SELECT count(*) FROM chunks) AS chunks, "
                    "(SELECT count(*) FROM chunk_embeddings) AS embeddings, "
                    "(SELECT version_num FROM alembic_version) AS revision"
                )
            )
        ).one()
        await conn.rollback()
    return {
        "scratch_schemas_left": left,
        "documents": int(row.documents),
        "chunks": int(row.chunks),
        "embeddings": int(row.embeddings),
        "alembic_revision": str(row.revision),
        "clean": left == 0,
    }


def _merge(fresh: dict[str, Any]) -> dict[str, Any]:
    """Keep the passes from earlier restarts, replace everything else.

    The sizing has to be verified at two settings of `max_locks_per_transaction`, and
    changing that setting needs a server restart — so the two halves of the answer cannot be
    produced by one process. Each run keys its pass by the setting it observed and merges;
    a pass therefore carries its own timestamp, and a stale one says so instead of being
    silently averaged into a fresh one.
    """
    if not REPORT.exists():
        return fresh
    try:
        previous = json.loads(REPORT.read_text())
    except json.JSONDecodeError:
        return fresh
    passes = previous.get("passes", {}) if isinstance(previous, dict) else {}
    if isinstance(passes, dict):
        merged = dict(passes)
        merged.update(fresh["passes"])
        fresh["passes"] = merged
    return fresh


async def _run(partitions: list[int]) -> int:
    owner = create_async_engine(settings.database_owner_url)
    app = create_async_engine(settings.database_url)
    started = time.perf_counter()
    report: dict[str, Any] = {
        "bar": {
            "sizing": (
                "A value for max_locks_per_transaction counts as verified only if "
                f"{CONCURRENCY} request-shaped transactions hold their locks simultaneously "
                "at the stage-02 modulus with none raising OutOfMemory, AND the default 64 "
                "is shown to fail the same test on the same schema. A pass with no paired "
                "failure is indistinguishable from a measurement that never ran."
            ),
            "prepared_statements": (
                "plan_cache_mode = auto may be left alone only if the generic plan, once "
                f"adopted, executes within {GENERIC_TOLERANCE}x the custom plan's settled "
                "median. Beyond that the mode has to be pinned, because the switch happens "
                "on the sixth execution of a connection that has already served five good "
                "ones and nothing in the product reports it."
            ),
            "set_before_the_run": True,
        }
    }
    pass_key = "unknown"

    try:
        async with owner.connect() as conn:
            server = await _server(conn)
            pass_key = str(server["max_locks_per_transaction"])
            production = await _production_relations(conn)
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
            # The live arms below run against `public` under RLS, so they need a tenant that
            # actually holds rows. A synthetic one would return nothing, and an empty result
            # set is exactly the null result this directory refuses to publish: it cannot be
            # told apart from a measurement that never ran.
            live_tenant = str(
                (
                    await conn.execute(
                        text(
                            "SELECT tenant_id FROM chunk_embeddings "
                            "GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
                        )
                    )
                ).scalar_one()
            )
            # Every label that tenant has, which is what an administrator's context carries.
            # Without it the policy on `chunks` admits only unlabelled rows and the live arms
            # measure an empty result set — see `_context`.
            live_labels = ",".join(
                str(row[0])
                for row in await conn.execute(
                    text("SELECT id::text FROM access_labels WHERE tenant_id = :t ORDER BY 1"),
                    {"t": live_tenant},
                )
            )
            await conn.rollback()

        report["production_relations"] = production
        expected = int(str(production["relations_per_partition_pair_expected"]))
        print(
            f"max_locks_per_transaction = {server['max_locks_per_transaction']}, "
            f"{server['nominal_lock_slots']} nominal slots, "
            f"shared_memory_size = {server.get('shared_memory_size')} MB"
        )
        print(f"schema declares {expected} relations per partition-pair\n")

        params: dict[str, object] = {
            "model": model,
            "version": version,
            "embedding": vector,
            "limit": WANTED,
        }
        queried = _tenant(0)

        await _drop(owner)
        async with owner.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
            await conn.execute(text(f"GRANT USAGE ON SCHEMA {SCHEMA} TO zenith_app"))
            rows = await _assign(conn, (model, version))
        print(f"{rows:,} vectors assigned across {POPULATED} synthetic tenants\n")

        rungs: dict[str, object] = {}
        for count in partitions:
            build_s = await _build(owner, count, (model, version))
            async with owner.connect() as conn:
                relations = await _relations(conn)
                await conn.rollback()
            measured = await _rung(app, params, queried)
            rungs[str(count)] = {
                "partitions": count,
                "build_s": build_s,
                "relations_in_schema": relations,
                "relations_per_partition_pair_in_catalogue": round(relations / count, 3),
                **measured,
            }
            print(
                f"  {count:>5} partitions  built in {build_s}s  {relations} relations  "
                f"locks/request {measured.get('locks_for_request')}  "
                f"planning {measured.get('planning_ms')} ms",
                flush=True,
            )
            if count != partitions[-1]:
                await _teardown(owner)

        # The slope between the two rungs, which is the whole point of having two. Every
        # per-transaction constant — the virtual transaction id, the two parents, the
        # `documents` relations a real hydrate would add — is in both readings and cancels.
        slope: float | None = None
        if len(partitions) >= 2:
            low, high = str(partitions[0]), str(partitions[-1])
            first, last = rungs[low], rungs[high]
            if isinstance(first, dict) and isinstance(last, dict):
                lo_locks, hi_locks = first.get("locks_for_request"), last.get("locks_for_request")
                if isinstance(lo_locks, int) and isinstance(hi_locks, int):
                    slope = (hi_locks - lo_locks) / (partitions[-1] - partitions[0])
        measured_relations = round(slope) if slope else expected

        print("\n  concurrency at the top rung:", flush=True)
        async with owner.connect() as conn:
            cluster = await _cluster(conn)
            await conn.rollback()
        if cluster["other_scratch_schemas"] or int(str(cluster["locks_held_by_others"])) > 200:
            print(f"    NOTE: not alone on this database — {json.dumps(cluster)}", flush=True)
        concurrency = await _concurrency(settings.database_url, params, queried, CONCURRENCY_LADDER)

        print("\n  prepared statements, partitioned:", flush=True)
        prepared_partitioned = await _prepared(
            app, _dense(SCHEMA), params, queried, f"p{partitions[-1]}"
        )
        # Through the driver as well as through an explicit PREPARE, and on the partitioned
        # schema rather than only on `public`. This is where `auto` has a reason to switch:
        # planning a 256-partition Append is tens of milliseconds, which is exactly the saving
        # the planner weighs against a worse generic plan.
        driver_partitioned = await _driver(app, _dense(SCHEMA), params, queried, "")

        report["passes"] = {
            pass_key: {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "server": server,
                "rungs": rungs,
                "locks_per_partition_pair_measured": slope,
                "measured_matches_schema": measured_relations == expected,
                "cluster_at_measurement": cluster,
                "concurrency_at_top_rung": {"partitions": partitions[-1], **concurrency},
                "prepared_partitioned": prepared_partitioned,
                "prepared_partitioned_verdict": _verdict(prepared_partitioned),
                "driver_partitioned": driver_partitioned,
            }
        }

        # The live arms run against `public`, so they belong to the installation rather than
        # to a pass — but they are re-measured every run, because the setting under test
        # changes nothing about them and a stale copy would be indistinguishable from a fresh
        # one. Only the last run's survives, which is the correct number for the settings the
        # report ends at.
        print("\n  prepared statements, live and unpartitioned:", flush=True)
        report["live_tenant"] = live_tenant
        report["live_labels"] = live_labels
        report["driver_live"] = await _driver(app, _LIVE_DENSE, params, live_tenant, live_labels)
        report["prepared_live"] = await _prepared_live(app, params, live_tenant, live_labels)
        report["prepared_live_verdict"] = _verdict(report["prepared_live"])
        # Bar 2, decided on the arm that is the product. The `_prepared` arms above are a
        # proxy with a `text` parameter and they fail it; `_driver` runs `dense()` through the
        # application engine with psycopg binding it, and Postgres 17's own counters say
        # whether a generic plan was ever chosen. A bar answered by the proxy when the product
        # is measurable beside it is the shape of number this directory keeps having to
        # retract.
        switched = bool(
            driver_partitioned["chose_a_generic_plan"]
            or report["driver_live"]["chose_a_generic_plan"]
        )
        decided = bool(
            driver_partitioned["reached_the_generic_decision"]
            and report["driver_live"]["reached_the_generic_decision"]
        )
        report["prepared_statement_verdict"] = {
            "decided_on": ["driver_partitioned", "driver_live"],
            "the_driver_prepares": report["driver_live"]["server_prepares"],
            "first_execution_that_prepared": report["driver_live"]["first_execution_that_prepared"],
            "reached_the_generic_decision": decided,
            "product_chose_a_generic_plan": switched,
            "passes_bar_2": decided and not switched,
            "why_the_prepare_arms_disagree": (
                "The `prepared_*` arms declare the query vector as a `text` parameter, so the "
                "cast to halfvec(1024) is real work a generic plan repeats per row. psycopg "
                "sends it untyped and the server infers halfvec from the cast context "
                "(pg_prepared_statements.parameter_types), so the product pays nothing for "
                "it. The proxy overstates and must not be quoted for the product."
            ),
            "what_is_not_saved": (
                "The switch never happens, so the planning cost of a partitioned Append is "
                "paid on every execution rather than once per connection. That is the cost to "
                "carry into stage 02, and it is the opposite of the risk this arm was written "
                "to find."
            ),
        }
        top = rungs[str(partitions[-1])]
        demand = int(str(top["locks_for_request"])) if isinstance(top, dict) else 0
        report["sizing"] = _sizing(
            demand or measured_relations * partitions[-1],
            measured_relations,
            partitions[-1],
            CONCURRENCY,
            int(str(server["max_connections"])),
            int(str(server["max_prepared_transactions"])),
        )
    finally:
        await _drop(owner)
        report["verification"] = await _verify(owner)
        report["took_s"] = round(time.perf_counter() - started, 1)
        report["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        report.setdefault("passes", {})
        REPORT.write_text(json.dumps(_merge(report), indent=2) + "\n")
        await app.dispose()
        await owner.dispose()

    print(f"\nWritten to {REPORT.name} (pass max_locks_per_transaction={pass_key})")
    # Non-zero when the scratch schema survived. A sweep that left its own tables behind on a
    # database holding a customer's corpus has failed, whatever else it measured.
    return 0 if report["verification"]["clean"] else 1


#: How `python -m eval` finds this sweep. Declared here rather than listed in `__main__.py`,
#: so adding a measurement is adding a file and nothing else.
COMMAND = "lock-budget"
USAGE = "lock-budget [--partitions 64,256]"


def cli(argv: list[str]) -> int:
    counts = list(PARTITIONS)
    if "--partitions" in argv:
        counts = [int(part) for part in argv[argv.index("--partitions") + 1].split(",")]
    return asyncio.run(_run(counts))


def run() -> int:
    return cli(sys.argv[2:])
