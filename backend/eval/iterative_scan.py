# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""What iterative scan costs and buys on the **unscoped** path — the hot one.

`tenant-scale.json` measured the dense stage and found it silently short: with RLS as the
only predicate, the HNSW scan returns its `ef_search` nearest neighbours from a graph
shared by every tenant and the policies discard afterwards, so the dense half came back
with 43.1 of 50 candidates tenant-wide and *nothing at all* for 5 of 42 questions.

What that measurement cannot say is whether any of it reaches a reader. Between the dense
half and the page sit the lexical half, the exact-identifier signal, RRF, the leader floor
and a cross-encoder, and any of them can absorb a stage-level loss or amplify it. So this
answers the question that actually decides the change, and answers it in two layers
because they resolve different things:

1. **The dense stage**, through `search.dense` itself, under the application role with the
   real policies in force. Rows returned, questions given nothing, and milliseconds. The
   numbers here are single-digit milliseconds and they are resolvable.

2. **End to end**, the same questions through `SearchService`, scored by `eval/live.py`'s
   credit rule so the number is comparable to `live-recall.json` and `rerank-depth.json`.
   Recall here is deterministic and is the finding. Latency here is **not** resolvable —
   an interactive search is dominated by two TEI round trips, and the run-to-run spread of
   an unchanged configuration is larger than everything the dense stage costs in total.
   That is why every individual pass is kept in the report: the honest reading of the
   end-to-end p95 is the spread, not the point estimate.

Three arms. `off` is not a hypothetical — it is exactly what the unscoped path did before
this change, since the `SET LOCAL` was conditional on a document scope. `strict_order` is
here because `relaxed_order` can return rows slightly out of distance order and this
pipeline fuses on *positions*; the arm is what says whether that theoretical cost is worth
paying to avoid.

The end-to-end arms are **interleaved** rather than run in blocks. Measured in blocks the
first arm gets a cold cross-encoder and the last one a warm machine, and the drift lands
entirely on whichever arm was unlucky — which is precisely the shape of a false regression.

**Read-only.** Every transaction this opens is `READ ONLY` and rolled back; the
end-to-end passes are searches. Nothing writes.

    docker compose exec -T api python -m eval iterative-scan
"""

import asyncio
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.hardware import active as active_profile
from app.features.embeddings.client import MODEL, VERSION, TeiClient
from app.features.embeddings.space import SHIPPED
from app.features.retrieval import search as search_module
from app.features.retrieval.search import CANDIDATES
from eval.harness import Installation, installation, score
from eval.questions import Question

REPORT = Path(__file__).parent / "iterative-scan.json"

#: `off` reproduces the shipped behaviour of the unscoped path before this change rather
#: than quoting it. The other two are the only values pgvector 0.8 offers.
ARMS = ("off", "relaxed_order", "strict_order")
#: Whole end-to-end passes per arm, interleaved. Small because each pass is every question
#: through a cross-encoder; enough that a p95 which moved by a pass's worth of noise cannot
#: be read as a regression.
REPEATS = 3
#: Repeats of the dense query alone, where a millisecond means something. The fastest is
#: taken: the question is what the scan costs, not what the container was doing at the time.
DENSE_REPEATS = 5


async def _defaults_and_bound() -> dict[str, object]:
    """Whether `hnsw.max_scan_tuples` can bind on this corpus at all.

    The worry that argues for setting it is that iterative scan is unbounded, so a query
    where almost nothing passes the filter walks the whole graph with the 10 s
    `statement_timeout` as the only stop. pgvector 0.8 is **not** unbounded — it stops at
    `max_scan_tuples` — so the question is not "what should the bound be" but "does the
    shipped default already sit below this graph". Read rather than assumed, through the
    application role, in a read-only transaction.
    """
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        # Touching a vector loads pgvector's module, and loading it is what defines these
        # GUCs. Read before that and Postgres answers from a placeholder rather than from
        # the extension, which is a different number that looks exactly like this one.
        await conn.execute(text("SELECT '[1]'::vector"))
        defaults = {
            name: (await conn.execute(text(f"SHOW {name}"))).scalar_one()
            for name in ("hnsw.iterative_scan", "hnsw.max_scan_tuples", "hnsw.ef_search")
        }
        await conn.rollback()
    await engine.dispose()

    owner = create_async_engine(settings.database_owner_url)
    async with owner.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        graph = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM chunk_embeddings "
                    "WHERE embedding_model = :m AND embedding_version = :v"
                ),
                {"m": MODEL, "v": VERSION},
            )
        ).scalar_one()
        await conn.rollback()
    await owner.dispose()

    bound = int(defaults["hnsw.max_scan_tuples"])
    return {
        "defaults": defaults,
        "graph_embeddings": graph,
        "max_scan_tuples_can_bind": bound < graph,
    }


async def _dense_stage(
    where: Installation, vectors: list[list[float]], questions: list[Question]
) -> dict[str, dict[str, object]]:
    """`search.dense` itself, under the application role with the policies in force.

    The shipped function rather than a copy of its SQL: `dense` is what sets `ef_search`
    and `iterative_scan`, and a re-implementation here would measure this file's idea of
    the query instead of the one that runs. It takes a session and uses it only to
    `execute`, which a connection does too — that is the whole of the type mismatch.

    The RLS context is set the way `tenant_session` sets it, on a connection opened
    `READ ONLY` so a mistake in laboratory code cannot reach the corpus it is measuring.
    """
    ef = active_profile().hnsw_ef_search
    engine = create_async_engine(settings.database_url)
    results: dict[str, dict[str, object]] = {}

    for value in ARMS:
        original = search_module.ITERATIVE_SCAN
        search_module.ITERATIVE_SCAN = value
        returned: list[int] = []
        times: list[float] = []
        empty: list[str] = []
        try:
            for question, vector in zip(questions, vectors, strict=True):
                async with engine.connect() as conn:
                    await conn.execute(text("SET TRANSACTION READ ONLY"))
                    await conn.execute(
                        text("SELECT set_config('zenith.tenant_id', :t, true)"),
                        {"t": str(where.tenant)},
                    )
                    await conn.execute(
                        text("SELECT set_config('zenith.label_ids', :l, true)"),
                        {"l": ",".join(str(label) for label in where.labels)},
                    )
                    fastest: float | None = None
                    rows: list[tuple[UUID, float]] = []
                    for _ in range(DENSE_REPEATS):
                        started = time.perf_counter()
                        rows = await search_module.dense(
                            conn,  # type: ignore[arg-type]
                            vector,
                            SHIPPED,
                            CANDIDATES,
                            ef,
                        )
                        elapsed = (time.perf_counter() - started) * 1000
                        fastest = elapsed if fastest is None else min(fastest, elapsed)
                    await conn.rollback()
                returned.append(len(rows))
                times.append(fastest or 0.0)
                if not rows:
                    empty.append(question.id)
        finally:
            search_module.ITERATIVE_SCAN = original

        results[value] = {
            "rows_returned_mean": round(statistics.mean(returned), 2),
            "rows_returned_min": min(returned),
            "short_of_ceiling": sum(1 for count in returned if count < CANDIDATES),
            "returned_nothing": empty,
            "median_ms": round(statistics.median(times), 2),
            "p95_ms": round(sorted(times)[int(len(times) * 0.95) - 1], 2),
            "max_ms": round(max(times), 2),
        }
        print(f"  {value:<14} {json.dumps(results[value])}", flush=True)

    await engine.dispose()
    return results


async def _end_to_end(where: Installation) -> dict[str, dict[str, Any]]:
    """`REPEATS` interleaved passes per arm, each a full search of every question.

    The arm is selected by rebinding the module constant, because that is where the value
    lives: `dense` reads `ITERATIVE_SCAN` at call time, so this runs the shipped code path
    with one literal changed and nothing else.
    """
    passes: dict[str, list[dict[str, object]]] = {value: [] for value in ARMS}
    for repeat in range(REPEATS):
        for value in ARMS:
            original = search_module.ITERATIVE_SCAN
            search_module.ITERATIVE_SCAN = value
            try:
                measured = await score(where)
            finally:
                search_module.ITERATIVE_SCAN = original
            passes[value].append(measured)
            print(f"  pass {repeat + 1} {value:<14} {json.dumps(measured)}", flush=True)

    def median_of(runs: list[dict[str, object]], field: str) -> float | None:
        values = [float(run[field]) for run in runs if run[field] is not None]  # type: ignore[arg-type]
        return round(statistics.median(values), 4) if values else None

    return {
        value: {
            # Recall is deterministic for a fixed setting, so these are the value itself
            # unless an arm is unstable — in which case `passes` is where that shows.
            "headline_recall_at_8": median_of(runs, "headline_recall_at_8"),
            "recall_at_8_all": median_of(runs, "recall_at_8_all"),
            "recall_at_1_all": median_of(runs, "recall_at_1_all"),
            "mean_rank": median_of(runs, "mean_rank"),
            "median_ms": median_of(runs, "median_ms"),
            "p95_ms": median_of(runs, "p95_ms"),
            "p95_ms_range": [
                min(int(run["p95_ms"]) for run in runs),  # type: ignore[arg-type]
                max(int(run["p95_ms"]) for run in runs),  # type: ignore[arg-type]
            ],
            "degraded": max(int(run["degraded"]) for run in runs),  # type: ignore[arg-type]
            "missed": sorted({qid for run in runs for qid in run["missed"]}),  # type: ignore[union-attr]
            "passes": runs,
        }
        for value, runs in passes.items()
    }


async def _run() -> int:
    where = await installation()
    if not where.questions:
        print("No question's document is in this corpus — nothing to measure.")
        return 1

    profile = active_profile()
    print(
        f"{where.space.n} embeddings, top-{CANDIDATES}, ef_search "
        f"{profile.hnsw_ef_search}, profile {profile.name}"
    )
    print(f"{len(where.questions)} scorable questions, {REPEATS} passes per arm\n")

    bound = await _defaults_and_bound()
    print(f"scan bound: {json.dumps(bound)}\n")

    client = TeiClient()
    vectors = [await client.embed_query(question.question) for question in where.questions]

    print("dense stage, unscoped, under the real policies:")
    stage = await _dense_stage(where, vectors, where.questions)

    print("\nend to end, unscoped, scored as live.py scores:")
    reached = await _end_to_end(where)

    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "profile": profile.name,
        "hnsw_ef_search": profile.hnsw_ef_search,
        "rerank_candidates": profile.rerank_candidates,
        "candidates": CANDIDATES,
        "corpus_chunks": where.space.n,
        "scored": len(where.questions),
        "repeats": REPEATS,
        "dense_repeats": DENSE_REPEATS,
        "scope": "unscoped — no document filter, so RLS is the only predicate",
        "scan_bound": bound,
        "dense_stage": stage,
        "end_to_end": reached,
    }
    REPORT.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nWritten to {REPORT.name}")
    return 0


#: How `python -m eval` finds this sweep. Declared here rather than listed in
#: `__main__.py`, so adding a measurement is adding a file and nothing else.
COMMAND = "iterative-scan"
USAGE = "iterative-scan"


def run() -> int:
    return asyncio.run(_run())
