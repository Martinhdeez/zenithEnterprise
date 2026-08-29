# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""What partitioning costs the queries that cannot prune.

`partition-shape.json` measured the queries that *can*. Every tenant-scoped read prunes to
one partition — `partition-rls-policy-pruning.sql` proves it survives the real policy, the
real role and a per-partition copy of the predicate — and gets faster. That is the argument
for ADR 0009 and it is not in question here.

**This file measures the other half of the schema**: the statements that carry no tenant, and
the statements that carry one and prune anyway less than they appear to. At the planned
modulus of 256 the first group goes from one scan to 256. Those paths are invisible in a demo
— a diagnostic, a purge, a background trigger — and they are the ones that hurt in
production, because the ceiling stage 01 found is **locks, not planning time**: a statement
takes `relations x partitions` locks and, past the shared lock table, fails with `OutOfMemory`
*during planning*. That is an HTTP 500, not a slow answer.

## Reads and writes are different, and that is the finding

A read prunes at executor startup when its key comes from `zenith_current_tenant()`, which is
`STABLE`. `EXPLAIN` says `Subplans Removed` and one partition is scanned.

**A write does not.** `UPDATE` and `DELETE` on a partitioned table list their result relations
at *plan* time, and plan-time pruning needs a constant. A `STABLE` function is not one. So a
statement whose only tenant qualifier is the RLS policy — which is every write this product
makes to `chunks`, because the policy is `tenant_id = zenith_current_tenant()` — scans one
partition and **opens all of them for writing**. At MODULUS 256, on the same delete over the
same rows: 19 locks when the tenant arrives as a bound parameter, 2,059 when it arrives from
the function (`reingest_clear_chunks` against `reingest_clear_chunks_bound`, below).

That is why every write arm below is measured three ways — no tenant, the tenant from
`zenith_current_tenant()`, and the tenant as a bound parameter — and why `partitions_scanned`
and `partitions_targeted` are two columns rather than one. A report with a single column would
have called the middle arm fixed.

## The surface, and why these shapes

Enumerated by grep, not by memory — `docs/partitioning-unpruned-surface.md` is the list and
`tests/integration/test_unpruned_query_audit.py` is what stops it growing quietly. What
survived the enumeration as something that actually touches `chunks` or `chunk_embeddings`
without pruning:

1. **`zenith diagnose`'s content check** — `SELECT count(*)` over both tables through
   `owner_session`, deliberately unfiltered so an operator sees their own installation rather
   than a policy's empty answer. Measured against two candidate replacements: the planner's
   `pg_class.reltuples` estimate, and an exact count taken one tenant at a time.
2. **`zenith_lexical_search`** — migration 0022, `SECURITY DEFINER`, so no policy applies and
   the tenant clause lives *inside* the Tantivy query as `paradedb.term('tenant_id', ...)`.
   A `@@@` operand is not a partition-key qualifier, so nothing prunes. This one is on the hot
   path of every search, which is what separates it from the rest.
3. **`zenith_sync_chunk_labels`** — migration 0003, also `SECURITY DEFINER`, also no tenant:
   `UPDATE chunks SET label_ids = ... WHERE document_id = NEW.id`, fired once per document
   whose labels change and once per document during a purge.
4. **Re-ingestion's `_clear_previous`** — carries a tenant, from the policy, and is therefore
   the arm that shows what a policy-supplied tenant is worth to a write.
5. **The purge cascade** — `DELETE FROM documents WHERE tenant_id = ...` reaches `chunks`
   through a foreign key on `document_id`, which carries no tenant. Measured with the
   single-column key the schema has today and with the composite `(document_id, tenant_id)`
   that a partitioned `chunks` makes available.

## The bars, written before the run

`BARS` below is the predicate each arm is held to, stated so a run cannot be read backwards
into whatever it produced. `verdict` in the report is the comparison. A failed bar stays in
the file.

## What is built

`zenith_unpruned`: a `docs`/`chk`/`emb` set carrying the installation's real index count on
`chunks` — primary key, BM25, GIN on `label_ids`, GIN on `tsv`, btree on `tenant_id` — because
locks are taken per *relation* and an index is a relation. `chunk_embeddings` is built with
its primary key only; its HNSW index is projected rather than built, since building 256 of
them measures nothing this file asks about.

Rows come from the real corpus, reassigned to synthetic tenants **by document**, so a
document's passages all live in one partition. A document straddling two is not a state this
product can reach, and a purge arm built on one would measure a cascade the schema forbids.

**Read-only with respect to the corpus.** The only writes are into `zenith_unpruned`, which is
dropped in a `finally` and verified gone. Every mutating arm runs inside a transaction that is
rolled back.

    docker compose exec -T api python -m eval unpruned-queries [--partitions 64,256]
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
import time
from pathlib import Path
from typing import Any, TypedDict

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.core.config import settings

COMMAND = "unpruned-queries"
USAGE = "unpruned-queries [--partitions 64,256]"

REPORT = Path(__file__).parent / "unpruned-queries.json"

#: One schema, one name, dropped in a `finally`. Deliberately not `zenith_partshape` and not
#: `zenith_locks_probe`: three sweeps are being written against this same database from three
#: worktrees, and the only thing keeping them out of each other's way is that none of them
#: invents a second scratch name.
SCHEMA = "zenith_unpruned"

#: The rungs. 256 is the modulus stage 02 is being built to; 64 is on the ladder so the report
#: can say whether a cost is linear in the partition count or worse, which one rung cannot.
#: The flat control — no partitioning — runs first, and without it every number here would be
#: a number with nothing to compare against.
COUNTS: tuple[int, ...] = (64, 256)

#: Synthetic tenants that hold rows. Eight, as in `partition-pruning.sql` and
#: `partition_shape.py`, so "one partition" and "every partition" are far apart in any plan.
POPULATED = 8

#: Partitions created per transaction. Every `CREATE TABLE ... PARTITION OF` holds its locks
#: to the end of its transaction, so an unbatched build at 256 exhausts the same table this
#: sweep is measuring — and fails during setup, which would look like a result.
BATCH = 64

#: Timed repeats per arm. The median is reported, not the minimum: these are costs paid once
#: per call on paths nobody retries, so the middle of the distribution is the honest figure.
REPEATS = 5

#: The term the lexical arm searches for. Common enough to match inside every populated
#: tenant — an arm that returns nothing measures a plan that never ran, which is the failure
#: this repository has hit three times in a week.
LEXICAL_TERM = "de"

#: Passages the lexical arm asks for, as `app/features/retrieval/search.py` asks for them.
LEXICAL_WANT = 50


# --- Reading a plan --------------------------------------------------------------------

_PLANNING = re.compile(r"Planning Time: ([\d.]+) ms")
_EXECUTION = re.compile(r"Execution Time: ([\d.]+) ms")
_REMOVED = re.compile(r"Subplans Removed: (\d+)")
_TRIGGER = re.compile(r"Trigger (?:for constraint )?(.+?):(?: time=([\d.]+))? calls=(\d+)")

#: The partitioned relations of the scratch schema, by name: partitions are `c0..cN` and
#: `e0..eN`, and the flat control's equivalents end in `_flat`. Anchored on ` on ` so an index
#: name that embeds a table name — `c3_pkey` — does not count as a relation; `\b` refuses it
#: because `_` is a word character.
#:
#: `docs` is deliberately absent. It is never partitioned, so counting it would make the
#: control and the rungs report different numbers for the same plan — and the purge arms would
#: read as though they touched a partition when what they touch is a trigger. Their bar is on
#: locks for exactly that reason.
_LEAF = r"(?:[\w]+\.)?(chk_flat|emb_flat|c\d+|e\d+)\b"

#: A node that *reads* a relation. `never executed` is excluded on purpose: a subplan the
#: executor skipped is in the plan and was not run, and counting it would report runtime
#: pruning as no pruning at all.
_SCANNED = re.compile(rf"(?:Seq|Index Only|Index|Bitmap Heap|Tid|Sample|Custom) Scan.* on {_LEAF}")

#: A node that *writes* a relation. This is the list `ExecInitModifyTable` opens and locks,
#: and it is decided at plan time — which is why it does not shrink for a `STABLE` key even
#: when the scan beneath it does.
_TARGETED = re.compile(rf"^(?:Update|Delete|Insert|Merge) on {_LEAF}")


def _read_plan(lines: list[str]) -> dict[str, object]:
    """What a partitioned plan has to state about itself.

    Four facts, each answering a question the others cannot.

    `partitions_scanned` is how many partitions were actually read. `partitions_targeted` is
    how many a `ModifyTable` opened for writing. **They differ, and the difference is this
    file's main result**: a write whose tenant comes from `zenith_current_tenant()` scans one
    partition and opens every one, because result relations are chosen at plan time and a
    `STABLE` function is not a plan-time constant.

    `subplans_removed` is the *runtime* pruning evidence and only appears under `ANALYZE`.
    It is collected as a list, one entry per `Append`, because a single number would report
    one relation's pruning as the whole query's — and an empty list is not a zero, it means
    the node was absent, which can equally mean nothing pruned or that everything was pruned
    at plan time. `partitions_scanned` is what tells those apart, which is why no bar in this
    file is written on `Subplans Removed`.

    `paradedb_scans` is counted separately because ADR 0002 records the exact failure this
    file has to be able to see: with the predicate outside the Tantivy query the custom scan
    silently does not run, every `paradedb.score(id)` comes back NULL, and search keeps
    answering with everything ranked equally. A plan with a custom scan and a plan without one
    take the same time and mean opposite things.

    `triggers` carries the cascade. A `DELETE` on a parent shows none of its foreign-key work
    in the plan tree — the referential action runs in an `AFTER` trigger — so a purge measured
    by its plan alone reports the cheap half. The trigger line names the constraint once and
    not the partitions, so how wide the cascade reached is read off the lock count instead.
    """
    joined = "\n".join(lines)
    planning = _PLANNING.search(joined)
    execution = _EXECUTION.search(joined)
    removed = [int(match) for match in _REMOVED.findall(joined)]
    stripped = [line.strip().removeprefix("->  ").removeprefix("Parallel ") for line in lines]

    scanned = sorted(
        {
            match
            for line in stripped
            if "never executed" not in line
            for match in _SCANNED.findall(line)
        }
    )
    targeted = sorted({match for line in stripped for match in _TARGETED.findall(line)})
    triggers = [
        {"constraint": name, "ms": float(ms) if ms else None, "calls": int(calls)}
        for name, ms, calls in _TRIGGER.findall(joined)
    ]
    return {
        "planning_ms": float(planning.group(1)) if planning else None,
        "execution_ms": float(execution.group(1)) if execution else None,
        "partitions_scanned": len(scanned),
        "partitions_targeted": len(targeted),
        "subplans_removed": removed,
        "subplans_removed_total": sum(removed),
        "paradedb_scans": sum(1 for line in stripped if "ParadeDB Scan" in line),
        "leaf_scans": [line for line in stripped if _SCANNED.search(line)][:3],
        "triggers": triggers[:3],
    }


async def _explain(conn: AsyncConnection, statement: str, params: dict[str, object]) -> list[str]:
    rows = await conn.execute(
        text(f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY ON) {statement}"), params
    )
    return [str(row[0]) for row in rows]


async def _locks(conn: AsyncConnection) -> int:
    """Locks held by this backend right now.

    Counted inside the transaction that ran the statement, because that is the only place the
    number exists — every one is released at commit. A partitioned statement takes a lock on
    every partition it opens and on every index of every partition, at plan time, before
    runtime pruning has removed anything. That is why this grows with the modulus even for a
    read that prunes, and why it is the number that decides how many such statements can be
    in flight at once.
    """
    return int(
        (
            await conn.execute(text("SELECT count(*) FROM pg_locks WHERE pid = pg_backend_pid()"))
        ).scalar_one()
    )


# --- The shapes ------------------------------------------------------------------------


class Shape(TypedDict):
    """One arm's statement and what a reader needs to know about it.

    A `TypedDict` rather than a bare dict because `params` has to stay `dict[str, object]`
    all the way to the driver. Inferred from an untyped literal it came out as
    `dict[bytes, bytes]`, which type-checks against nothing and reads as a mystery.
    """

    statement: str
    params: dict[str, object]
    #: Mutating arms get a fresh transaction per repeat and are rolled back.
    mutating: bool
    #: Whether this is a shape the product runs, or a candidate fix measured beside it.
    ships: bool
    why: str


def _shapes(chk: str, emb: str, docs: str) -> dict[str, Shape]:
    """Every arm, as the product writes it, with the scratch schema substituted and nothing
    else changed.

    `chk`, `emb` and `docs` are passed rather than fixed so the flat control and the
    partitioned rungs run character-identical SQL against differently shaped tables. A control
    running a different statement would measure the statement.

    Where an arm is a candidate fix rather than something that ships, `ships` is false and
    `why` says what it is a candidate for.
    """
    return {
        # --- `zenith diagnose`, `_content()` -------------------------------------------
        "diagnose_count_chunks": {
            "statement": f"SELECT count(*) FROM {SCHEMA}.{chk}",
            "params": {},
            "mutating": False,
            "ships": True,
            "why": (
                "app/core/diagnostics.py `_content` — owner_session, no tenant. Once per "
                "`zenith diagnose`, and `demo-check.sh` runs that before every demonstration."
            ),
        },
        "diagnose_count_embeddings": {
            "statement": f"SELECT count(*) FROM {SCHEMA}.{emb}",
            "params": {},
            "mutating": False,
            "ships": True,
            "why": "app/core/diagnostics.py `_content` — the second of the two partitioned tables.",
        },
        "diagnose_count_estimate": {
            "statement": (
                "WITH RECURSIVE tree AS ("
                "  SELECT to_regclass(:parent)::oid AS oid"
                "  UNION ALL"
                "  SELECT i.inhrelid FROM pg_inherits i JOIN tree t ON i.inhparent = t.oid) "
                "SELECT coalesce(sum(c.reltuples), 0)::bigint "
                "FROM tree JOIN pg_class c ON c.oid = tree.oid WHERE c.relkind = 'r'"
            ),
            "params": {"parent": f"{SCHEMA}.{chk}"},
            "mutating": False,
            "ships": False,
            "why": (
                "Candidate fix: the planner's own estimate, read from the catalogue. Scans no "
                "partition at any modulus, and is an estimate — the report states its error "
                "against the exact count rather than leaving the trade to prose."
            ),
        },
        "diagnose_count_per_tenant": {
            # The key arrives from `zenith_current_tenant()` in every scoped arm here, never
            # as a literal. That is not decoration: the function is `STABLE`, so the planner
            # cannot fold it and pruning has to happen at executor startup. A literal prunes
            # at plan time, which is the easier case and not the one the product runs.
            "statement": (
                f"SELECT count(*) FROM {SCHEMA}.{chk} WHERE tenant_id = zenith_current_tenant()"
            ),
            "params": {},
            "mutating": False,
            "ships": False,
            "why": (
                "Candidate fix: exact, one tenant at a time. The figure to multiply by the "
                "tenant count — not to read as a whole-installation cost."
            ),
        },
        # --- `zenith_lexical_search`, migration 0022 -----------------------------------
        "lexical_as_shipped": {
            "statement": (
                f"SELECT c.id, paradedb.score(c.id) AS score FROM {SCHEMA}.{chk} c "
                "WHERE c.id @@@ paradedb.boolean(must => ARRAY["
                "  paradedb.match('text', :term),"
                "  paradedb.term('tenant_id', zenith_current_tenant()),"
                "  paradedb.boolean(should => ARRAY[paradedb.term('unlabelled', true)])]) "
                "ORDER BY paradedb.score(c.id) DESC LIMIT :want"
            ),
            "params": {"term": LEXICAL_TERM, "want": LEXICAL_WANT},
            "mutating": False,
            "ships": True,
            "why": (
                "migration 0022 `zenith_lexical_search`, SECURITY DEFINER so no policy "
                "applies. The tenant clause is a Tantivy term, and a `@@@` operand is not a "
                "partition-key qualifier. On the hot path of every search."
            ),
        },
        "lexical_scoped": {
            # The tenant stays inside the Tantivy query. The added qualifier is *redundant*
            # for correctness and exists only so the planner can see the partition key — which
            # is why it does not reintroduce ADR 0002's failure. The predicate has not moved
            # out of the search; a second copy of it has been put where the planner looks.
            "statement": (
                f"SELECT c.id, paradedb.score(c.id) AS score FROM {SCHEMA}.{chk} c "
                "WHERE c.tenant_id = zenith_current_tenant() "
                "  AND c.id @@@ paradedb.boolean(must => ARRAY["
                "  paradedb.match('text', :term),"
                "  paradedb.term('tenant_id', zenith_current_tenant()),"
                "  paradedb.boolean(should => ARRAY[paradedb.term('unlabelled', true)])]) "
                "ORDER BY paradedb.score(c.id) DESC LIMIT :want"
            ),
            "params": {"term": LEXICAL_TERM, "want": LEXICAL_WANT},
            "mutating": False,
            "ships": False,
            "why": (
                "Candidate fix: the same Tantivy query with a redundant SQL qualifier on the "
                "partition key beside it. Checked for the custom scan and for NULL scores, "
                "because ADR 0002's failure mode is a plan that still answers."
            ),
        },
        # --- `zenith_sync_chunk_labels`, migration 0003 --------------------------------
        "label_sync_as_shipped": {
            "statement": (
                f"UPDATE {SCHEMA}.{chk} SET label_ids = ARRAY[CAST(:label AS uuid)] "
                "WHERE document_id = CAST(:document AS uuid)"
            ),
            "params": {"label": "00000000-0000-0000-0000-000000000999", "document": "__document__"},
            "mutating": True,
            "ships": True,
            "why": (
                "migration 0003 `zenith_sync_chunk_labels`, SECURITY DEFINER. Fires once per "
                "document whose labels change, and once per document during a purge."
            ),
        },
        "label_sync_stable_tenant": {
            "statement": (
                f"UPDATE {SCHEMA}.{chk} SET label_ids = ARRAY[CAST(:label AS uuid)] "
                "WHERE document_id = CAST(:document AS uuid) "
                "  AND tenant_id = zenith_current_tenant()"
            ),
            "params": {"label": "00000000-0000-0000-0000-000000000999", "document": "__document__"},
            "mutating": True,
            "ships": False,
            "why": (
                "The obvious fix, and the one that is only half a fix. A `STABLE` key prunes "
                "the scan at executor startup and does not shrink the result-relation list, "
                "so the reads get cheap and the locks do not. This arm exists to make that "
                "visible rather than to be adopted."
            ),
        },
        "label_sync_bound_tenant": {
            "statement": (
                f"UPDATE {SCHEMA}.{chk} SET label_ids = ARRAY[CAST(:label AS uuid)] "
                "WHERE document_id = CAST(:document AS uuid) "
                "  AND tenant_id = CAST(:tenant AS uuid)"
            ),
            "params": {
                "label": "00000000-0000-0000-0000-000000000999",
                "document": "__document__",
                "tenant": "__tenant__",
            },
            "mutating": True,
            "ships": False,
            "why": (
                "Candidate fix: the tenant as a bound parameter, which the planner may fold "
                "into a custom plan and prune on. The trigger already has `NEW.tenant_id` in "
                "hand — this is one column on a WHERE clause."
            ),
        },
        # --- re-ingestion ---------------------------------------------------------------
        "reingest_clear_chunks": {
            "statement": (
                f"DELETE FROM {SCHEMA}.{chk} WHERE document_id = CAST(:document AS uuid) "
                "  AND tenant_id = zenith_current_tenant()"
            ),
            "params": {"document": "__document__"},
            "mutating": True,
            "ships": True,
            "why": (
                "app/features/ingestion/pipeline.py `_clear_previous`, under `tenant_session` "
                "so the tenant clause is the policy's and is therefore `STABLE`. This is the "
                "shape every write this product makes to `chunks` has."
            ),
        },
        "reingest_clear_chunks_bound": {
            "statement": (
                f"DELETE FROM {SCHEMA}.{chk} WHERE document_id = CAST(:document AS uuid) "
                "  AND tenant_id = CAST(:tenant AS uuid)"
            ),
            "params": {"document": "__document__", "tenant": "__tenant__"},
            "mutating": True,
            "ships": False,
            "why": (
                "Candidate fix: the same delete with the tenant bound. `TenantContext` has it "
                "already, so nothing has to be looked up to write it down."
            ),
        },
        # --- the purge cascade ------------------------------------------------------------
        "purge_documents": {
            "statement": f"DELETE FROM {SCHEMA}.{docs} WHERE tenant_id = zenith_current_tenant()",
            "params": {},
            "mutating": True,
            "ships": True,
            "why": (
                "app/features/system/purge.py — `DELETE FROM documents WHERE tenant_id`, "
                "reaching `chunks` through a foreign key on `document_id` that carries no "
                "tenant. Measured with the single-column key the schema has today."
            ),
        },
        "purge_documents_composite_fk": {
            "statement": f"DELETE FROM {SCHEMA}.{docs} WHERE tenant_id = zenith_current_tenant()",
            "params": {},
            "mutating": True,
            "ships": False,
            "why": (
                "Candidate fix: the same purge with `chunks` referencing "
                "`documents (id, tenant_id)` instead of `documents (id)`. The referential "
                "action is a statement Postgres writes, and it can only carry the partition "
                "key if the key is in the constraint. The constraint is swapped in place on "
                "the same rung, so the rows, the partitions and the planner state are "
                "identical and the constraint is the only difference."
            ),
        },
    }


# --- The bars, in the file before the run ------------------------------------------------
#
# `arm -> (claim, rule)`. The claim is what a reader is owed; the rule is what `_verdict`
# decides it with. A bar rewritten after a run is not a bar, so a failure stays here and the
# report says `failed`.

BARS: dict[str, tuple[str, str]] = {
    "diagnose_count_chunks": (
        "scans every partition, so its cost is linear in the modulus",
        "scans_everything",
    ),
    "diagnose_count_embeddings": (
        "scans every partition, for the same reason",
        "scans_everything",
    ),
    "diagnose_count_estimate": (
        "scans no partition at all, and takes the same locks at every modulus",
        "scans_nothing",
    ),
    "diagnose_count_per_tenant": ("scans exactly one partition", "scans_one"),
    "lexical_as_shipped": (
        "scans every partition — a Tantivy term is not a partition-key qualifier — with one "
        "ParadeDB custom scan on each",
        "scans_everything",
    ),
    "lexical_scoped": (
        "scans one partition AND keeps the custom scan AND returns rows with no NULL score: "
        "all three, because ADR 0002's failure mode is a plan that still answers",
        "prunes_and_scores",
    ),
    "label_sync_as_shipped": (
        "scans every partition and opens every one for writing",
        "scans_and_targets_everything",
    ),
    "label_sync_stable_tenant": (
        "scans one partition and still opens every one for writing, because result relations "
        "are chosen at plan time and `zenith_current_tenant()` is STABLE",
        "scans_one_targets_everything",
    ),
    "label_sync_bound_tenant": (
        "scans one partition and opens one for writing",
        "scans_one_targets_one",
    ),
    "reingest_clear_chunks": (
        "scans one partition and still opens every one for writing — the same half-pruning as "
        "the label sync, and it is what every policy-scoped write to `chunks` will do",
        "scans_one_targets_everything",
    ),
    "reingest_clear_chunks_bound": (
        "scans one partition and opens one for writing",
        "scans_one_targets_one",
    ),
    "purge_documents": (
        "takes at least one lock per partition of `chunks`: the cascade's foreign key is on "
        "`document_id` alone, so the statement Postgres writes for it carries no tenant",
        "locks_at_least_one_per_partition",
    ),
    # Restated once, before the ladder ran and after the eight-partition smoke run, because
    # the first wording was arithmetically false rather than unmet: it demanded "fewer locks
    # than there are partitions", which no statement can satisfy at a modulus of eight when it
    # legitimately locks a parent, one partition and five of its indexes. What is actually
    # being claimed is a comparison between two arms on the same rung, and that is what it now
    # says. The bar the ladder was held to is this one; the smoke run's numbers are in the
    # commit message rather than in the report, because they measured eight partitions and
    # this file reports 64 and 256.
    "purge_documents_composite_fk": (
        "takes fewer locks than `purge_documents` does on the same rung, and a count that "
        "does not grow with the modulus, because the constraint now carries the partition key",
        "locks_fewer_than_shipped",
    ),
}


def _verdict(rule: str, partitions: int, arm: dict[str, object], rung: dict[str, Any]) -> str:
    """Hold one measured arm to its bar. `unmeasured` is a third answer and not a pass.

    Every rule reads `partitions_scanned`, `partitions_targeted` or the lock count — all three
    counted off the plan or off `pg_locks` — and none reads `Subplans Removed`. Pruning that
    happens at plan time removes the subplan before `EXPLAIN` can report having removed it, so
    a rule written on `Subplans Removed` calls a perfectly pruned plan unpruned. The first
    version of this file did that and reported four failures that were its own.
    """
    if "error" in arm:
        return "error"
    scanned = arm.get("partitions_scanned")
    targeted = arm.get("partitions_targeted")
    locks = arm.get("locks_for_query")
    if not isinstance(scanned, int) or not isinstance(targeted, int) or not isinstance(locks, int):
        return "unmeasured"

    if rule == "scans_everything":
        return "met" if scanned == partitions else "failed"
    if rule == "scans_nothing":
        return "met" if scanned == 0 else "failed"
    if rule == "scans_one":
        return "met" if scanned == 1 else "failed"
    if rule == "scans_and_targets_everything":
        return "met" if scanned == partitions and targeted == partitions else "failed"
    if rule == "scans_one_targets_everything":
        return "met" if scanned == 1 and targeted == partitions else "failed"
    if rule == "scans_one_targets_one":
        return "met" if scanned == 1 and targeted == 1 else "failed"
    if rule == "locks_at_least_one_per_partition":
        return "met" if locks >= partitions else "failed"
    if rule == "locks_fewer_than_shipped":
        # An unpartitioned control has one shape, so the two purge arms are the same statement
        # against the same schema and comparing them says nothing. `not_applicable` rather than
        # a pass: a bar that cannot be tested has not been met.
        if partitions == 1:
            return "not_applicable"
        shipped = rung.get("purge_documents")
        reference = shipped.get("locks_for_query") if isinstance(shipped, dict) else None
        if not isinstance(reference, int):
            return "unmeasured"
        return "met" if locks < reference else "failed"
    if rule == "prunes_and_scores":
        custom_scans = arm.get("paradedb_scans")
        returned = arm.get("returned")
        return (
            "met"
            if scanned == 1
            and custom_scans == 1
            and arm.get("null_scores") == 0
            and isinstance(returned, int)
            and returned > 0
            else "failed"
        )
    return "unmeasured"


# --- Running one arm ---------------------------------------------------------------------


async def _prepare(conn: AsyncConnection, tenant: str) -> None:
    """The session the caller would have.

    `zenith.tenant_id` is set even though every arm runs as the owner and no policy applies:
    the scoped arms read it through `zenith_current_tenant()`, which is how the product
    supplies a tenant and — being `STABLE` — is the harder pruning case.

    `statement_timeout` is raised well above the product's: at 256 partitions planning alone
    can approach it, and a rung that timed out would be reported as a failure to plan rather
    than as the planning cost it is. Serial, for the reason `partition_shape.py` gives — a
    parallel plan over hundreds of partitions asks for more shared memory than this container
    has and fails with `DiskFull`, which names the wrong resource.

    `paradedb.enable_custom_scan` is on because migration 0022 turns it off for the whole
    database and back on for `zenith_lexical_search` alone. Measuring the lexical arm without
    it would measure a query the product does not run.
    """
    await conn.execute(
        text("SELECT set_config('zenith.tenant_id', :tenant, true)"), {"tenant": tenant}
    )
    await conn.execute(text("SET LOCAL statement_timeout = '300s'"))
    await conn.execute(text("SET LOCAL max_parallel_workers_per_gather = 0"))
    await conn.execute(text("SET LOCAL paradedb.enable_custom_scan = on"))


async def _arm(owner: AsyncEngine, shape: Shape, tenant: str) -> dict[str, object]:
    """One shape at one rung: its plan, its locks, its latency and what it returned.

    Failure is caught and recorded rather than raised. Running out of the shared lock table
    *is* the measurement at the top of a ladder, and an exception there would throw away every
    rung below it.

    A mutating arm gets a fresh transaction per repeat and is rolled back, so the second
    repeat is not measuring a statement with nothing left to do. That is not tidiness: a
    `DELETE` repeated inside one transaction reports the cost of finding no rows.
    """
    statement = shape["statement"]
    params = dict(shape["params"])
    plan: dict[str, object] = {}

    async with owner.connect() as conn:
        try:
            await _prepare(conn, tenant)
            before = await _locks(conn)
            plan = _read_plan(await _explain(conn, statement, params))
            after = await _locks(conn)
        except Exception as exc:  # noqa: BLE001 - the failure is the result at the top rungs
            return {"error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"}
        finally:
            await conn.rollback()

    times: list[float] = []
    extra: dict[str, object] = {}
    rows = 0
    for _ in range(REPEATS):
        async with owner.connect() as conn:
            try:
                await _prepare(conn, tenant)
                started = time.perf_counter()
                result = await conn.execute(text(statement), params)
                columns = list(result.keys()) if result.returns_rows else []
                returned = result.all() if result.returns_rows else []
                times.append((time.perf_counter() - started) * 1000)
                rows = len(returned)
                # A count arm's answer is the point of the arm. Kept so the report can state
                # what the `reltuples` estimate is wrong by, instead of describing the trade in
                # prose and asking the reader to take it on trust.
                if len(returned) == 1 and len(returned[0]) == 1 and isinstance(returned[0][0], int):
                    extra["value"] = returned[0][0]
                if returned and "score" in columns:
                    values = [row.score for row in returned]
                    extra["null_scores"] = sum(1 for value in values if value is None)
                    extra["top_score"] = (
                        round(float(values[0]), 4) if values[0] is not None else None
                    )
            except Exception as exc:  # noqa: BLE001
                return {**plan, "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"}
            finally:
                await conn.rollback()

    ordered = sorted(times)
    return {
        **plan,
        **extra,
        "locks_for_query": after - before,
        "returned": rows,
        "median_ms": round(statistics.median(times), 3),
        "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
        "min_ms": round(min(times), 3),
    }


# --- Building the scratch schema -----------------------------------------------------------

#: Relations built here per partition, against what the installation's schema has. `chunks` is
#: built with its full index set — primary key, BM25, GIN on `label_ids`, GIN on `tsv`, btree
#: on `tenant_id` — because a lock is taken per relation and an index is a relation.
#: `chunk_embeddings` is built with its primary key only; its HNSW index is projected rather
#: than built, since building 256 of them measures nothing this file asks about.
BUILT_RELATIONS = {"chk": 6, "emb": 2}
PRODUCTION_RELATIONS = {"chk": 6, "emb": 3}

_PARENT_DDL = """
CREATE TABLE {schema}.docs (
  id        uuid NOT NULL,
  tenant_id uuid NOT NULL,
  filename  varchar NOT NULL,
  PRIMARY KEY (id),
  UNIQUE (id, tenant_id)
);
CREATE TABLE {schema}.chk (
  id          uuid NOT NULL,
  tenant_id   uuid NOT NULL,
  document_id uuid NOT NULL REFERENCES {schema}.docs (id) ON DELETE CASCADE,
  label_ids   uuid[] NOT NULL DEFAULT '{{}}',
  unlabelled  boolean GENERATED ALWAYS AS (label_ids = '{{}}'::uuid[]) STORED,
  text        text NOT NULL,
  tsv         tsvector GENERATED ALWAYS AS (to_tsvector('zenith_text', text)) STORED,
  PRIMARY KEY (id, tenant_id)
) PARTITION BY HASH (tenant_id);
CREATE TABLE {schema}.emb (
  chunk_id          uuid NOT NULL,
  tenant_id         uuid NOT NULL,
  embedding_model   varchar NOT NULL,
  embedding_version varchar NOT NULL,
  PRIMARY KEY (chunk_id, tenant_id, embedding_model, embedding_version),
  FOREIGN KEY (chunk_id, tenant_id)
    REFERENCES {schema}.chk (id, tenant_id) ON DELETE CASCADE
) PARTITION BY HASH (tenant_id);
"""

#: The flat control. Same columns and the same index set, unpartitioned, with the primary and
#: foreign keys the installation has today — `chunks (id)` and `chunk_embeddings (chunk_id)`,
#: neither of which can survive partitioning, because a partitioned table's unique constraints
#: must contain the partition key. That difference is itself a finding for stage 02 and is
#: recorded in the report rather than smoothed over here.
_FLAT_DDL = """
CREATE TABLE {schema}.docs_flat (
  id        uuid NOT NULL,
  tenant_id uuid NOT NULL,
  filename  varchar NOT NULL,
  PRIMARY KEY (id),
  UNIQUE (id, tenant_id)
);
CREATE TABLE {schema}.chk_flat (
  id          uuid NOT NULL,
  tenant_id   uuid NOT NULL,
  document_id uuid NOT NULL REFERENCES {schema}.docs_flat (id) ON DELETE CASCADE,
  label_ids   uuid[] NOT NULL DEFAULT '{{}}',
  unlabelled  boolean GENERATED ALWAYS AS (label_ids = '{{}}'::uuid[]) STORED,
  text        text NOT NULL,
  tsv         tsvector GENERATED ALWAYS AS (to_tsvector('zenith_text', text)) STORED,
  PRIMARY KEY (id),
  UNIQUE (id, tenant_id)
);
CREATE TABLE {schema}.emb_flat (
  chunk_id          uuid NOT NULL,
  tenant_id         uuid NOT NULL,
  embedding_model   varchar NOT NULL,
  embedding_version varchar NOT NULL,
  PRIMARY KEY (chunk_id, embedding_model, embedding_version),
  FOREIGN KEY (chunk_id) REFERENCES {schema}.chk_flat (id) ON DELETE CASCADE
);
"""

_BM25 = (
    "USING bm25 (id, text, tenant_id, label_ids, unlabelled) "
    "WITH (key_field = 'id', text_fields = "
    '\'{"text": {"tokenizer": {"type": "en_stem", "lowercase": true}}}\')'
)


async def _indexes(conn: AsyncConnection, table: str) -> None:
    """The installation's index set on `chunks`, created on the parent so every partition
    inherits a copy.

    Copied from `app/features/documents/model.py` rather than paraphrased. Getting the count
    wrong would report a lock floor that is not this product's.
    """
    await conn.execute(text(f"CREATE INDEX ON {SCHEMA}.{table} {_BM25}"))
    await conn.execute(text(f"CREATE INDEX ON {SCHEMA}.{table} USING gin (label_ids)"))
    await conn.execute(text(f"CREATE INDEX ON {SCHEMA}.{table} USING gin (tsv)"))
    await conn.execute(text(f"CREATE INDEX ON {SCHEMA}.{table} (tenant_id)"))


async def _partitions(conn: AsyncConnection, count: int) -> None:
    """`count` HASH partitions of both tables, in batches.

    Batched because every `CREATE TABLE ... PARTITION OF` holds its locks to the end of its
    transaction, and an unbatched build at 256 exhausts the table this sweep exists to
    measure — failing during setup, which would look like a result and is not one.
    """
    for low in range(0, count, BATCH):
        high = min(low + BATCH, count)
        await conn.execute(
            text(
                "DO $do$ DECLARE i int; BEGIN "
                f"FOR i IN {low}..{high - 1} LOOP "
                f"  EXECUTE 'CREATE TABLE {SCHEMA}.c' || i "
                f"       || ' PARTITION OF {SCHEMA}.chk FOR VALUES WITH "
                f"(MODULUS {count}, REMAINDER ' || i || ')'; "
                f"  EXECUTE 'CREATE TABLE {SCHEMA}.e' || i "
                f"       || ' PARTITION OF {SCHEMA}.emb FOR VALUES WITH "
                f"(MODULUS {count}, REMAINDER ' || i || ')'; "
                "END LOOP; END $do$;"
            )
        )


#: Rows are assigned to synthetic tenants **by document**, not by chunk. A document whose
#: passages straddle two partitions is not a state this product can reach, and a purge arm
#: built on one would measure a cascade the schema forbids.
_ASSIGN = f"""
CREATE TABLE {SCHEMA}.assign_docs AS
SELECT d.id,
       ('00000000-0000-0000-0000-'
        || lpad(((row_number() OVER (ORDER BY d.id) - 1) % {POPULATED})::text, 12, '0'))::uuid
         AS tenant_id,
       d.filename
FROM documents d;
CREATE UNIQUE INDEX ON {SCHEMA}.assign_docs (id);
CREATE TABLE {SCHEMA}.assign AS
SELECT c.id, a.tenant_id, c.document_id, c.text
FROM chunks c JOIN {SCHEMA}.assign_docs a ON a.id = c.document_id;
CREATE INDEX ON {SCHEMA}.assign (tenant_id)
"""


async def _load(conn: AsyncConnection, chk: str, emb: str, docs: str) -> None:
    await conn.execute(
        text(
            f"INSERT INTO {SCHEMA}.{docs} SELECT id, tenant_id, filename FROM {SCHEMA}.assign_docs"
        )
    )
    await conn.execute(
        text(
            f"INSERT INTO {SCHEMA}.{chk} (id, tenant_id, document_id, text) "
            f"SELECT id, tenant_id, document_id, text FROM {SCHEMA}.assign"
        )
    )
    await conn.execute(
        text(
            f"INSERT INTO {SCHEMA}.{emb} (chunk_id, tenant_id, embedding_model, embedding_version) "
            f"SELECT id, tenant_id, 'BAAI/bge-m3', '1' FROM {SCHEMA}.assign"
        )
    )


#: Partitions dropped per statement, and much smaller than `BATCH`.
#:
#: A `DROP TABLE` takes an `AccessExclusiveLock` on the table and on every one of its five
#: indexes, so a batch of 64 is around four hundred locks in one statement — and the lock
#: table is **the whole cluster's**, not this backend's. One run of this ladder failed here,
#: with `OutOfMemory` raised by the teardown itself, while two other measurement branches were
#: building partitioned schemas on the same database. The schema then survived, which is the
#: one outcome this file is not allowed to produce.
#:
#: Sixteen, with a one-at-a-time fallback below. Slower and it finishes.
DROP_BATCH = 16


async def _drop_partitions(conn: AsyncConnection) -> None:
    """Drop every partition in the scratch schema, in small batches, retrying singly.

    The fallback is not belt-and-braces. The failure it recovers from is the very pressure
    this sweep measures, arriving from another backend, and a teardown that gives up under it
    leaves 2,075 relations behind for somebody else to find.
    """
    while True:
        names = [
            str(row[0])
            for row in await conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid = c.relnamespace "
                    "WHERE n.nspname = :s AND c.relkind = 'r' AND c.relispartition LIMIT :n"
                ),
                {"s": SCHEMA, "n": DROP_BATCH},
            )
        ]
        if not names:
            return
        targets = ", ".join(f"{SCHEMA}.{name}" for name in names)
        try:
            await conn.execute(text(f"DROP TABLE IF EXISTS {targets} CASCADE"))
        except Exception:  # noqa: BLE001 - the batch is a convenience, the drop is not
            for name in names:
                await conn.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.{name} CASCADE"))


async def _drop(owner: AsyncEngine) -> None:
    """Drop the scratch schema in batches.

    `DROP SCHEMA ... CASCADE` is one transaction taking an `AccessExclusiveLock` on every
    object in it. At 256 partitions with five indexes each that is more locks than the shared
    table holds, so the tidy-up fails and the schema survives — the one outcome this file is
    not allowed to produce. Children first, in batches, then the parents and the schema.
    """
    async with owner.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await _drop_partitions(conn)
        await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))


async def _teardown_rung(owner: AsyncEngine) -> None:
    """Drop the rung's partitioned tables, keeping `assign` and the flat control."""
    async with owner.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await _drop_partitions(conn)
        await conn.execute(
            text(f"DROP TABLE IF EXISTS {SCHEMA}.emb, {SCHEMA}.chk, {SCHEMA}.docs CASCADE")
        )


async def _swap_foreign_key(owner: AsyncEngine, chk: str, docs: str, *, composite: bool) -> None:
    """Point `chunks`' foreign key at `documents (id)` or at `documents (id, tenant_id)`.

    On a partitioned parent this validates against every partition, which is cheap here and
    would not be on a real corpus — a note that belongs in the migration rather than in the
    measurement.
    """
    async with owner.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        existing = [
            str(row[0])
            for row in await conn.execute(
                text(
                    "SELECT conname FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "JOIN pg_namespace n ON n.oid = t.relnamespace "
                    "WHERE n.nspname = :s AND t.relname = :t AND c.contype = 'f' "
                    "AND c.confrelid = to_regclass(:parent)::oid"
                ),
                {"s": SCHEMA, "t": chk, "parent": f"{SCHEMA}.{docs}"},
            )
        ]
        for name in existing:
            await conn.execute(text(f"ALTER TABLE {SCHEMA}.{chk} DROP CONSTRAINT {name}"))
        columns = "(document_id, tenant_id)" if composite else "(document_id)"
        references = "(id, tenant_id)" if composite else "(id)"
        await conn.execute(
            text(
                f"ALTER TABLE {SCHEMA}.{chk} ADD FOREIGN KEY {columns} "
                f"REFERENCES {SCHEMA}.{docs} {references} ON DELETE CASCADE"
            )
        )


# --- What the numbers mean ------------------------------------------------------------------


def _pressure(partitions: int, arm: dict[str, object], slots: int) -> dict[str, object]:
    """What a lock count means, which is not what a single timing suggests.

    `max_locks_per_transaction` does not cap a transaction. It sizes one table the whole
    cluster draws from — `max_locks_per_transaction * max_connections` entries — so a
    statement holding N locks has used N of the *installation's* slots for the length of its
    transaction. The number that matters is therefore how many such statements can be in
    flight, and that is what a single-statement benchmark cannot see.

    Sizing that setting is `chore/partition-lock-budget`'s work and is deliberately not
    attempted here. This reports what each shape costs; what the budget should be is a
    different question with a different owner.
    """
    locks = arm.get("locks_for_query")
    if not isinstance(locks, int) or locks <= 0:
        return {}
    return {
        "locks_per_partition": round(locks / partitions, 2),
        "share_of_lock_table": round(locks / slots, 4),
        "concurrent_statements_at_nominal_slots": slots // locks,
    }


async def _postgres(conn: AsyncConnection) -> tuple[dict[str, object], int]:
    rows = (
        await conn.execute(
            text(
                "SELECT name, setting FROM pg_settings WHERE name IN ("
                "'max_locks_per_transaction', 'max_connections', 'max_prepared_transactions', "
                "'shared_buffers', 'work_mem', 'plan_cache_mode', 'enable_partition_pruning', "
                "'server_version')"
            )
        )
    ).all()
    found = {str(row.name): str(row.setting) for row in rows}
    slots = int(found["max_locks_per_transaction"]) * (
        int(found["max_connections"]) + int(found["max_prepared_transactions"])
    )
    return {
        **found,
        "lock_table_slots": slots,
        "lock_table_note": (
            "max_locks_per_transaction * max_connections sizes one table shared by the whole "
            "cluster; it is not a per-transaction allowance. Sizing it belongs to "
            "`chore/partition-lock-budget`. This file reports only what each shape takes."
        ),
    }, slots


# --- The run ----------------------------------------------------------------------------


async def _rung(
    owner: AsyncEngine, partitions: int, slots: int, chk: str, emb: str, docs: str
) -> dict[str, object]:
    arms: dict[str, Any] = {}

    # The document and tenant the write arms address, read now rather than hard-coded: a
    # document id that does not exist turns an UPDATE into a measurement of finding nothing,
    # which is indistinguishable from a fast one.
    async with owner.connect() as conn:
        target = (
            await conn.execute(
                text(
                    f"SELECT document_id, tenant_id, count(*) AS rows FROM {SCHEMA}.assign "
                    "GROUP BY 1, 2 ORDER BY count(*) DESC LIMIT 1"
                )
            )
        ).one()
        await conn.rollback()
    tenant = str(target.tenant_id)

    for name, shape in _shapes(chk, emb, docs).items():
        params = dict(shape["params"])
        if params.get("document") == "__document__":
            params["document"] = str(target.document_id)
        if params.get("tenant") == "__tenant__":
            params["tenant"] = tenant

        if name == "purge_documents_composite_fk":
            await _swap_foreign_key(owner, chk, docs, composite=True)
        variant: Shape = {**shape, "params": params}
        measured = await _arm(owner, variant, tenant)
        if name == "purge_documents_composite_fk":
            await _swap_foreign_key(owner, chk, docs, composite=False)

        claim, rule = BARS[name]
        arms[name] = {
            **measured,
            "ships": shape["ships"],
            "why": shape["why"],
            "bar": claim,
            "verdict": _verdict(rule, partitions, measured, arms),
            "pressure": _pressure(partitions, measured, slots),
        }
        state = measured.get("error") or (
            f"{measured.get('median_ms')} ms  "
            f"scanned {measured.get('partitions_scanned')}  "
            f"targeted {measured.get('partitions_targeted')}  "
            f"{measured.get('locks_for_query')} locks"
        )
        print(f"    {name:<32} {arms[name]['verdict']:<10} {state}")

    return {
        "partitions": partitions,
        "document_rows": int(target.rows),
        "arms": arms,
    }


async def _run(counts: tuple[int, ...]) -> int:
    owner = create_async_engine(settings.database_owner_url)
    started = time.perf_counter()
    report: dict[str, Any] = {}

    try:
        async with owner.connect() as conn:
            server, slots = await _postgres(conn)
            passages = int((await conn.execute(text("SELECT count(*) FROM chunks"))).scalar_one())
            documents = int(
                (await conn.execute(text("SELECT count(*) FROM documents"))).scalar_one()
            )
            await conn.rollback()

        print(f"{passages:,} passages in {documents} documents, {POPULATED} synthetic tenants")
        print(f"lock table: {slots} slots\n")

        await _drop(owner)
        async with owner.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
            for statement in _ASSIGN.strip().split(";\n"):
                await conn.execute(text(statement))
            for statement in _FLAT_DDL.format(schema=SCHEMA).strip().rstrip(";").split(";\n"):
                await conn.execute(text(statement))
            await _indexes(conn, "chk_flat")
            await _load(conn, "chk_flat", "emb_flat", "docs_flat")
            await conn.execute(text(f"ANALYZE {SCHEMA}.chk_flat, {SCHEMA}.emb_flat"))
            exact = int(
                (await conn.execute(text(f"SELECT count(*) FROM {SCHEMA}.chk_flat"))).scalar_one()
            )

        print(f"flat control built, {exact:,} rows")
        report["control"] = await _rung(owner, 1, slots, "chk_flat", "emb_flat", "docs_flat")
        print()

        ladder: list[dict[str, object]] = []
        for count in counts:
            print(f"  {count} partitions")
            build_started = time.perf_counter()
            async with owner.connect() as conn:
                await conn.execution_options(isolation_level="AUTOCOMMIT")
                for statement in _PARENT_DDL.format(schema=SCHEMA).strip().rstrip(";").split(";\n"):
                    await conn.execute(text(statement))
                await _partitions(conn, count)
                await _indexes(conn, "chk")
                await _load(conn, "chk", "emb", "docs")
                await conn.execute(text(f"ANALYZE {SCHEMA}.chk, {SCHEMA}.emb"))
            build_s = round(time.perf_counter() - build_started, 1)
            print(f"    built in {build_s}s")

            rung = await _rung(owner, count, slots, "chk", "emb", "docs")
            rung["build_s"] = build_s
            ladder.append(rung)
            await _teardown_rung(owner)
            print()

        report["ladder"] = ladder
        report["postgres"] = server
        report["build"] = {
            "populated_tenants": POPULATED,
            "documents": documents,
            "rows": exact,
            "corpus_passages": passages,
            "relations_per_partition": BUILT_RELATIONS,
            "production_relations_per_partition": PRODUCTION_RELATIONS,
            "note": (
                "`chunks` is built with the installation's full index set, so its lock counts "
                "are the real ones. `chunk_embeddings` is built without its HNSW index — "
                "building 256 of them measures nothing this file asks about — so any figure "
                "involving it is a floor, and the third relation is projected rather than "
                "built."
            ),
            "schema_change_partitioning_forces": (
                "A partitioned table's unique constraints must contain the partition key, so "
                "`chunks (id)` becomes `chunks (id, tenant_id)` and every foreign key that "
                "references it — `chunk_embeddings.chunk_id` and `query_citations.chunk_id` — "
                "becomes composite. That is not optional and it is what makes the composite "
                "`documents` key measured here cheap to add at the same time."
            ),
        }
        report["estimate_error"] = _estimate_error(report)
        # One table, so a reader can see whether a cost is linear in the modulus without
        # reassembling it out of the arms. "Does not grow with the modulus" is a claim two of
        # the bars make, and a claim across rungs cannot be checked inside one.
        rungs: list[dict[str, Any]] = [report["control"], *ladder]
        report["locks_across_the_ladder"] = {
            name: {
                str(rung["partitions"]): rung["arms"].get(name, {}).get("locks_for_query")
                for rung in rungs
            }
            for name in BARS
        }
        report["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime())
        report["took_s"] = round(time.perf_counter() - started, 1)
        report["bars"] = {name: claim for name, (claim, _) in BARS.items()}
        REPORT.write_text(json.dumps(report, indent=2, default=str) + "\n")
        print(f"wrote {REPORT}")

        failed = [
            f"{rung['partitions']}/{name}"
            for rung in [report["control"], *ladder]
            for name, arm in rung["arms"].items()  # type: ignore[union-attr]
            if isinstance(arm, dict) and arm.get("verdict") in ("failed", "error", "unmeasured")
        ]
        print(f"bars not met: {', '.join(failed)}" if failed else "every bar met")
        return 0
    finally:
        await _drop(owner)
        async with owner.connect() as conn:
            left = (
                await conn.execute(
                    text("SELECT count(*) FROM pg_namespace WHERE nspname = :s"), {"s": SCHEMA}
                )
            ).scalar_one()
            await conn.rollback()
        print(f"scratch schema gone: {int(left) == 0}")
        await owner.dispose()


def _estimate_error(report: dict[str, Any]) -> dict[str, object]:
    """What the `reltuples` candidate is wrong by, at every rung.

    Stated rather than described. A fix that trades exactness for a constant cost has to say
    how much exactness, and after a fresh `ANALYZE` on a corpus this size the answer may well
    be "none" — which is a fact about this corpus and not a property of the method, so the
    report says so in the same breath.
    """
    rows: list[dict[str, object]] = []
    for rung in [report["control"], *report["ladder"]]:
        arms = rung["arms"]
        exact = arms.get("diagnose_count_chunks", {}).get("value")
        estimate = arms.get("diagnose_count_estimate", {}).get("value")
        if isinstance(exact, int) and isinstance(estimate, int):
            rows.append(
                {
                    "partitions": rung["partitions"],
                    "exact": exact,
                    "estimate": estimate,
                    "error": estimate - exact,
                    "relative_error": round(abs(estimate - exact) / exact, 6) if exact else None,
                }
            )
    return {
        "rungs": rows,
        "caveat": (
            "Measured immediately after `ANALYZE`, on a corpus of 13,549 passages that "
            "nothing was writing to. `reltuples` drifts between analyses in proportion to "
            "the write rate, so a small error here is not a promise about a busy "
            "installation. What it does promise is a cost that does not grow with the "
            "modulus, which is the property being bought."
        ),
    }


def run(counts: tuple[int, ...] = COUNTS) -> int:
    return asyncio.run(_run(counts))


def cli(argv: list[str]) -> int:
    counts = COUNTS
    if "--partitions" in argv:
        counts = tuple(int(part) for part in argv[argv.index("--partitions") + 1].split(","))
    return run(counts)
