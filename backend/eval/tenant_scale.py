# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""What the dense half loses when most of the graph belongs to someone else.

`ix_chunk_embeddings_hnsw_half` is **one graph for every tenant**. The scan walks it for the
`ef_search` nearest vectors and only then does anything check who may read them: the policy
on `chunk_embeddings` filters by tenant inside the index-scan node, and the join to `chunks`
applies the label policy after that. Neither is an index predicate — HNSW has none — so both
are post-filters over a fixed-size candidate set.

The consequence is arithmetic. Ask for `LIMIT 50` with `ef_search = 100` and the scan yields
100 rows; if a third of them survive the policy, the dense half returns 33 candidates instead
of 50 and says nothing. `pg_stat` reports nothing, the answer still arrives, `degraded` stays
false, and the index's own recall is perfect — it returned exactly the neighbours it was asked
for. What was lost is the neighbours it was never asked for.

This sweeps the one variable that decides it: **the fraction of the shared graph the caller
may read.** That fraction falls for two independent reasons, and the run measures both,
because they enter the plan at different nodes:

- **tenants** — one point per tenant holding embeddings, at that tenant's full label set
- **labels** — inside the largest tenant, a ladder of single-label scopes from its widest
  label down to its narrowest

A tenant count is the same variable read the other way round: `n` tenants of similar size
leave each of them roughly `1/n` of the graph, so every point also reports the
`equivalent_tenants` its fraction stands for. That mapping is the claim this file supports;
it is not a substitute for measuring a hundred real tenants, and it is labelled so nobody
reads it as one.

Both scan modes are measured at every point — with `hnsw.iterative_scan` off, and with it set
to `ITERATIVE_SCAN`. When this run was first written the second was the *candidate fix* and
`search.py` set it only for a document scope; it is now set unconditionally on the strength of
what this measured, so `default` is the arm that no longer ships. Both are kept, because the
gap between them is the finding and deleting the losing arm would delete the evidence.

## What migration 0025 moved, and it is not recall

fp16 made the index three times smaller, and a cheaper index wins the planner's comparison at
scopes where the fp32 one used to lose to a sequential scan. Two points crossed that line on
re-measurement: `Cyber Standards Archive` at 38.9% of the graph and `label legal/contracts` at
14.6%, both `exact` before and `hnsw` after. Their `default` figures fall off a cliff —
50 rows at recall 1.0000 to 10.23 at 0.1995 and 7.77 at 0.1553 — and none of that is the index
getting worse. An exact scan cannot lose rows to a post-filter because it has no candidate
budget to lose them from; those scopes were scoring 1.0000 by not using the index at all.

**The threshold this file describes moves down as the index shrinks**, so a smaller
representation buys capacity and widens the band of scopes exposed to the post-filter loss at
the same time. The `iterative` arm still returns 50 of 50 rows at every scope, which is why
the shipped path is unaffected and the end-to-end score below does not move.

**Read-only.** Every transaction is `READ ONLY` and rolled back; nothing here writes, and no
row, index or setting outlives the connection. Ground truth is taken under the *same* RLS
context as the measurement, with the index refused — so "what the caller should have seen" is
defined by the policy rather than by a role that bypasses it.

    docker compose exec -T api python -m eval tenant-scale
"""

import asyncio
import json
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.core.config import settings
from app.core.hardware import active as active_profile
from app.features.retrieval.search import CANDIDATES, ITERATIVE_SCAN
from eval.harness import installation, score
from eval.questions import load_questions

REPORT = Path(__file__).parent / "tenant-scale.json"

#: `search.py`'s dense query, verbatim apart from the document scope this run never passes.
#: Copied rather than imported because the point is to watch the plan the shipped SQL gets,
#: and `dense()` would insist on opening its own session with its own settings.
#:
#: Since migration 0025 that means `embedding_half` and a `halfvec(1024)` cast, because the
#: index is on the fp16 column and an operator class covers one type. Copied SQL that drifts
#: from the original is the standing hazard of the line above: ordering by `embedding` here
#: would plan a sequential scan, and this file would then report that a *sequential* scan
#: loses no rows to the policy — which is true, and the exact opposite of the finding.
DENSE = (
    "SELECT c.id, 1 - (e.embedding_half::halfvec(1024) "
    "<=> CAST(:embedding AS halfvec(1024))) AS score "
    "FROM chunk_embeddings e "
    "JOIN chunks c ON c.id = e.chunk_id "
    "WHERE e.embedding_model = :model AND e.embedding_version = :version "
    "ORDER BY e.embedding_half::halfvec(1024) <=> CAST(:embedding AS halfvec(1024)) LIMIT :limit"
)

#: Ground truth: the full-precision column, with every index refused. Not `DENSE`, for the
#: same reason `ef_search.py` separates the two — "what the caller should have seen" is the
#: truly nearest rows the policy entitles them to, so the baseline stays fp32 across migration
#: 0025 and every figure here remains comparable to the reports taken before it.
TRUTH = (
    "SELECT c.id, 1 - (e.embedding <=> CAST(:embedding AS vector)) AS score "
    "FROM chunk_embeddings e "
    "JOIN chunks c ON c.id = e.chunk_id "
    "WHERE e.embedding_model = :model AND e.embedding_version = :version "
    "ORDER BY e.embedding <=> CAST(:embedding AS vector) LIMIT :limit"
)

#: How many rows the caller may read at all, under the context in force. The denominator of
#: every fraction below, and the ceiling on what any scan could return.
ACCESSIBLE = (
    "SELECT count(*) FROM chunk_embeddings e JOIN chunks c ON c.id = e.chunk_id "
    "WHERE e.embedding_model = :model AND e.embedding_version = :version"
)

#: Enough repeats for the page cache to settle; the fastest is reported, for the same reason
#: `ef_search.py` reports the fastest — the question is what the scan costs, not what the
#: container happened to be doing.
REPEATS = 3

#: A label ladder wants distinct orders of magnitude, not thirteen neighbouring ones. Each
#: rung takes the widest remaining label at most this fraction of the previous rung's size.
LADDER_STEP = 0.5

#: The other remedy, and the one that needs no code: if the scan discards most of what it
#: fetches, fetch more. Swept at the widest scope, where the loss is, so that "turn the knob
#: you already have" is answered with a number rather than with an opinion.
#:
#: It ends at 1000 because pgvector ends there — `hnsw.ef_search` is bounded at 1000 and
#: rejects anything above it. That bound is the answer to whether this knob scales: it is a
#: fixed candidate budget against a graph that grows, so there is a corpus size past which
#: no setting of it is enough.
EF_SWEEP = (100, 200, 400, 800, 1000)


@dataclass(frozen=True, slots=True)
class Scope:
    """One RLS context to measure, and what it is meant to represent."""

    axis: str
    name: str
    tenant: UUID
    labels: tuple[UUID, ...]


async def _context(conn: AsyncConnection, scope: Scope) -> None:
    """Pin the policy variables, exactly as `set_rls_context` does for a request."""
    await conn.execute(
        text("SELECT set_config('zenith.tenant_id', :tenant, true)"), {"tenant": str(scope.tenant)}
    )
    await conn.execute(
        text("SELECT set_config('zenith.label_ids', :labels, true)"),
        {"labels": ",".join(str(label) for label in scope.labels)},
    )


async def _embed(questions: list[str]) -> list[list[float]]:
    async with httpx.AsyncClient(timeout=120.0) as client:
        vectors: list[list[float]] = []
        for question in questions:
            response = await client.post(
                f"{settings.tei_embed_url}/embed", json={"inputs": question, "truncate": True}
            )
            response.raise_for_status()
            vectors.append(response.json()[0])
    return vectors


async def _scopes(space: Any) -> tuple[list[Scope], int]:
    """Every context worth measuring, and the size of the graph they share.

    Read as the owner because this is the census — which tenants exist, and how big each
    one's slice of the graph is — and no tenant context can see it by construction. Nothing
    measured is read here; the measurements below all run under the application role.
    """
    owner = create_async_engine(settings.database_owner_url)
    async with owner.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        graph = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM chunk_embeddings "
                    "WHERE embedding_model = :model AND embedding_version = :version"
                ),
                {"model": space.model, "version": space.version},
            )
        ).scalar_one()
        tenants = (
            await conn.execute(
                text(
                    "SELECT c.tenant_id AS id, t.name, count(*) AS n "
                    "FROM chunk_embeddings e JOIN chunks c ON c.id = e.chunk_id "
                    "JOIN tenants t ON t.id = c.tenant_id "
                    "WHERE e.embedding_model = :model AND e.embedding_version = :version "
                    "GROUP BY 1, 2 ORDER BY n DESC"
                ),
                {"model": space.model, "version": space.version},
            )
        ).all()
        labels = (
            await conn.execute(text("SELECT id, tenant_id, name FROM access_labels ORDER BY name"))
        ).all()
        # The label ladder is built for the largest tenant: it is the one whose slice of the
        # graph is big enough for a label to subdivide meaningfully.
        biggest = tenants[0].id
        sizes = (
            await conn.execute(
                text(
                    "SELECT l.id, l.name, count(*) AS n "
                    "FROM chunk_embeddings e JOIN chunks c ON c.id = e.chunk_id "
                    "JOIN access_labels l ON l.id = ANY(c.label_ids) "
                    "WHERE c.tenant_id = :tenant "
                    "AND e.embedding_model = :model AND e.embedding_version = :version "
                    "GROUP BY 1, 2 ORDER BY n DESC"
                ),
                {"tenant": biggest, "model": space.model, "version": space.version},
            )
        ).all()
        await conn.rollback()
    await owner.dispose()

    by_tenant: dict[UUID, list[UUID]] = {}
    for label in labels:
        by_tenant.setdefault(label.tenant_id, []).append(label.id)

    scopes = [
        Scope(
            axis="tenant",
            name=f"{tenant.name} — every label",
            tenant=tenant.id,
            labels=tuple(by_tenant.get(tenant.id, [])),
        )
        for tenant in tenants
    ]

    ceiling = float("inf")
    for label in sizes:
        if label.n > ceiling:
            continue
        scopes.append(
            Scope(axis="label", name=f"label {label.name}", tenant=biggest, labels=(label.id,))
        )
        ceiling = label.n * LADDER_STEP
    return scopes, graph


async def _plan(conn: AsyncConnection, space: Any, vector: list[float]) -> dict[str, object]:
    """Which plan the caller actually got, and what the scan threw away.

    The finding this run exists to record is not a number but a shape, and the shape lives in
    the plan: an HNSW scan that emits `ef_search` rows and discards most of them, or — once
    the scope is narrow enough for the planner to prefer it — an exact scan that discards
    nothing and is correct by accident of the estimate rather than by design.
    """
    lines = (
        await conn.execute(
            text("EXPLAIN (ANALYZE, FORMAT JSON) " + DENSE),
            {
                "model": space.model,
                "version": space.version,
                "embedding": str(vector),
                "limit": CANDIDATES,
            },
        )
    ).scalar_one()
    plan = lines[0]["Plan"] if isinstance(lines, list) else json.loads(lines)[0]["Plan"]

    found: dict[str, object] = {"scan": "exact", "hnsw_rows": None, "rows_removed": None}

    stack = [plan]
    while stack:
        node = stack.pop()
        if "hnsw" in str(node.get("Index Name", "")):
            found = {
                "scan": "hnsw",
                "hnsw_rows": node.get("Actual Rows"),
                "rows_removed": node.get("Rows Removed by Filter"),
            }
        stack.extend(node.get("Plans", []))
    return found


async def _measure(
    conn: AsyncConnection,
    space: Any,
    ids: list[str],
    vectors: list[list[float]],
    truth: list[set[UUID]],
) -> dict[str, object]:
    """One scan mode at one scope: how much of the dense half survived, and how fast."""
    params = {"model": space.model, "version": space.version, "limit": CANDIDATES}
    returned: list[int] = []
    recalls: list[float] = []
    times: list[float] = []

    for vector, exact in zip(vectors, truth, strict=True):
        fastest: float | None = None
        got: set[UUID] = set()
        for _ in range(REPEATS):
            started = time.perf_counter()
            rows = await conn.execute(text(DENSE), {**params, "embedding": str(vector)})
            got = {row.id for row in rows}
            elapsed = (time.perf_counter() - started) * 1000
            fastest = elapsed if fastest is None else min(fastest, elapsed)
        returned.append(len(got))
        times.append(fastest or 0.0)
        # Against what the policy entitles the caller to, not against the corpus. A scope
        # holding fewer rows than `CANDIDATES` cannot return `CANDIDATES` and is not short.
        recalls.append(len(got & exact) / len(exact) if exact else 1.0)

    short = [
        question
        for question, count, exact in zip(ids, returned, truth, strict=True)
        if len(exact) > count
    ]
    return {
        "rows_returned_mean": round(statistics.mean(returned), 2),
        "rows_returned_min": min(returned),
        # A question the dense half answered with nothing at all, while the tenant held
        # thousands of passages it was entitled to. Named rather than counted, because a
        # claim this large has to be reproducible by hand.
        "returned_nothing": [
            question for question, count in zip(ids, returned, strict=True) if count == 0
        ],
        "short_of_ceiling": len(short),
        "short_questions": short,
        "dense_recall": round(statistics.mean(recalls), 4),
        "worst_query": round(min(recalls), 4),
        "worst_question": ids[recalls.index(min(recalls))],
        "median_ms": round(statistics.median(times), 2),
        "p95_ms": round(sorted(times)[int(len(times) * 0.95) - 1], 2),
    }


async def _point(
    app: Any, space: Any, scope: Scope, ids: list[str], vectors: list[list[float]], graph: int
) -> dict[str, object]:
    params = {"model": space.model, "version": space.version, "limit": CANDIDATES}
    profile = active_profile()

    # Ground truth, under this scope's own policy context with the index refused. The
    # comparison the whole run turns on is *these* rows against the ones the index produced,
    # so both sides must be entitled to exactly the same corpus.
    async with app.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        await _context(conn, scope)
        accessible = (await conn.execute(text(ACCESSIBLE), params)).scalar_one()
        await conn.execute(text("SET LOCAL enable_indexscan = off"))
        await conn.execute(text("SET LOCAL enable_bitmapscan = off"))
        truth = [
            {
                row.id
                for row in await conn.execute(text(TRUTH), {**params, "embedding": str(vector)})
            }
            for vector in vectors
        ]
        await conn.rollback()

    modes: dict[str, object] = {}
    plan: dict[str, object] = {}
    for mode in ("default", "iterative"):
        async with app.connect() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            await _context(conn, scope)
            await conn.execute(text(f"SET LOCAL hnsw.ef_search = {profile.hnsw_ef_search}"))
            if mode == "iterative":
                await conn.execute(text(f"SET LOCAL hnsw.iterative_scan = {ITERATIVE_SCAN}"))
            else:
                plan = await _plan(conn, space, vectors[0])
            modes[mode] = await _measure(conn, space, ids, vectors, truth)
            await conn.rollback()

    return {
        "axis": scope.axis,
        "scope": scope.name,
        "labels": [str(label) for label in scope.labels],
        "accessible": accessible,
        "fraction_of_graph": round(accessible / graph, 4),
        "equivalent_tenants": round(graph / accessible, 1) if accessible else None,
        # One question's plan, not the run's. Which plan the caller gets is the finding;
        # how many rows that particular vector lost to the filter is an illustration of it.
        "plan_sample": {"question": ids[0], **plan},
        **modes,
    }


async def _ef_sweep(
    app: Any, space: Any, scope: Scope, ids: list[str], vectors: list[list[float]]
) -> dict[str, object]:
    """What raising `hnsw_ef_search` alone recovers, at the scope that loses rows."""
    params = {"model": space.model, "version": space.version, "limit": CANDIDATES}
    async with app.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        await _context(conn, scope)
        await conn.execute(text("SET LOCAL enable_indexscan = off"))
        await conn.execute(text("SET LOCAL enable_bitmapscan = off"))
        truth = [
            {
                row.id
                for row in await conn.execute(text(TRUTH), {**params, "embedding": str(vector)})
            }
            for vector in vectors
        ]
        await conn.rollback()

    swept: dict[str, object] = {}
    for ef in EF_SWEEP:
        async with app.connect() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            await _context(conn, scope)
            await conn.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef)}"))
            swept[str(ef)] = await _measure(conn, space, ids, vectors, truth)
            await conn.rollback()
        print(f"  ef {ef:<5} {json.dumps(swept[str(ef)])[:120]}", flush=True)
    return swept


async def _end_to_end() -> dict[str, object]:
    """Does any of it reach the page?

    Scored by `eval/live.py`'s credit rule through `harness.score`, so the number sits beside
    `live-recall.json` and answers the only question a reader of this report actually has:
    the dense half is losing candidates — is the reader losing answers? Run at the shipped
    configuration, because that is the one nobody has been able to see through.
    """
    where = await installation()
    if not where.questions:
        return {"scored": 0, "note": "no question's document is in this corpus"}
    return {"scored": len(where.questions), **await score(where)}


async def _run() -> int:
    loaded = load_questions()
    ids = [question.id for question in loaded]
    questions = [question.question for question in loaded]
    if not questions:
        print("No questions loaded — nothing to measure.")
        return 1

    owner = create_async_engine(settings.database_owner_url)
    async with owner.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        space = (
            await conn.execute(
                text(
                    "SELECT embedding_model AS model, embedding_version AS version, "
                    "count(*) AS n FROM chunk_embeddings GROUP BY 1, 2 ORDER BY n DESC LIMIT 1"
                )
            )
        ).one()
        await conn.rollback()
    await owner.dispose()

    scopes, graph = await _scopes(space)
    profile = active_profile()
    print(
        f"{graph} embeddings in one HNSW graph, {len(scopes)} scopes, "
        f"top-{CANDIDATES}, ef_search {profile.hnsw_ef_search}, profile {profile.name}"
    )
    print(f"{len(questions)} questions\n")

    vectors = await _embed(questions)
    app = create_async_engine(settings.database_url)
    points: list[dict[str, object]] = []
    # The scope the `ef_search` sweep runs at: the largest one the planner still answers
    # from the HNSW index. Below it the planner falls back to an exact scan, where the knob
    # this sweep turns is not consulted at all and the sweep would measure nothing.
    widest: tuple[int, Scope] | None = None
    for scope in scopes:
        point = await _point(app, space, scope, ids, vectors, graph)
        points.append(point)
        default, plan = point["default"], point["plan_sample"]
        assert isinstance(default, dict) and isinstance(plan, dict)
        accessible = point["accessible"]
        assert isinstance(accessible, int)
        if plan["scan"] == "hnsw" and (widest is None or accessible > widest[0]):
            widest = (accessible, scope)
        print(
            f"  {str(point['scope'])[:34]:<34} {accessible:>6} rows "
            f"({point['fraction_of_graph']:.1%})  {plan['scan']:<5} "
            f"returned {default['rows_returned_mean']:>5}  recall {default['dense_recall']}",
            flush=True,
        )

    ef_sweep: dict[str, object] = {}
    if widest is not None:
        print(f"\nraising ef_search alone, at {widest[1].name}:")
        ef_sweep = await _ef_sweep(app, space, widest[1], ids, vectors)
    await app.dispose()

    print("\nend to end, scored as live.py scores:")
    reached = await _end_to_end()
    print(f"  {json.dumps(reached)[:200]}")

    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "profile": profile.name,
        "hnsw_ef_search": profile.hnsw_ef_search,
        "candidates": CANDIDATES,
        "iterative_scan": ITERATIVE_SCAN,
        "embedding_space": {"model": space.model, "version": space.version},
        "graph_embeddings": graph,
        "questions": len(questions),
        "repeats": REPEATS,
        "shipped_sets_iterative_scan": "unconditionally, on every dense query",
        "points": points,
        "ef_sweep": {"scope": widest[1].name if widest else None, "by_ef": ef_sweep},
        "end_to_end": reached,
    }
    REPORT.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nWritten to {REPORT.name}")
    return 0


#: How `python -m eval` finds this sweep. Declared here rather than listed in
#: `__main__.py`, so adding a measurement is adding a file and nothing else.
COMMAND = "tenant-scale"
USAGE = "tenant-scale"


def run() -> int:
    return asyncio.run(_run())
