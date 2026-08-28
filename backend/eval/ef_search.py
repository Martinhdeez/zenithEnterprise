# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""What `hnsw_ef_search` is worth on the machine you are running on.

The profile table carries a number per deployment and, until this existed, no evidence
for any of them. This sweeps the knob against the **live corpus** and answers two
different questions, because they have different answers:

1. *What does the index recover?* — the top-`CANDIDATES` the HNSW scan returns at each
   `ef_search`, against the exact nearest neighbours obtained with the index refused.
   This is where the floor came from: pgvector will not return more rows than
   `ef_search`, so a profile below the candidate count truncates the dense half without
   failing, logging, or being visible anywhere downstream.

2. *Does any of it reach the page?* — the same questions through `SearchService`,
   fusion and cross-encoder included, scored by `eval/live.py`'s credit rule so the
   number is comparable to `live-recall.json`. On this corpus the answer was no: every
   `ef_search` from 100 upward returned identical results. That negative is the point of
   keeping this file. The knob looks free and it is also worth nothing, and both halves
   have to be measurable or somebody will spend a week turning it.

**Read-only.** Every transaction is opened `READ ONLY`, so a mistake here cannot change
the installation it is measuring. It needs no token: it runs inside the API container and
builds a context directly, which is also why it is not a substitute for `live`.

    docker compose exec -T api python -m eval ef-search
"""

import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.hardware import active as active_profile
from app.features.retrieval.search import CANDIDATES
from eval.harness import installation, score

REPORT = Path(__file__).parent / "ef-search.json"

#: 40 is `low-spec`'s old value and is swept to keep the truncation reproducible; 800 is
#: past the point where the scan changes character on this machine, and is what makes the
#: knee visible rather than asserted.
SWEEP = (40, 100, 200, 400, 800)
#: Only from the floor upward. Below it the end-to-end run is measuring a truncation bug
#: rather than a tuning choice, which is a different finding and already made.
END_TO_END = (100, 200, 400)
#: Enough to let the page cache settle; the fastest of the repeats is reported, because
#: the question is what the scan costs and not what the container was doing at the time.
REPEATS = 5

NEIGHBOURS = (
    "SELECT c.id FROM chunk_embeddings e JOIN chunks c ON c.id = e.chunk_id "
    "WHERE e.embedding_model = :model AND e.embedding_version = :version "
    "ORDER BY e.embedding <=> CAST(:embedding AS vector) LIMIT :limit"
)


async def _embed(questions: list[str]) -> list[list[float]]:
    async with httpx.AsyncClient(timeout=120.0) as client:
        vectors = []
        for question in questions:
            response = await client.post(
                f"{settings.tei_embed_url}/embed", json={"inputs": question, "truncate": True}
            )
            response.raise_for_status()
            vectors.append(response.json()[0])
    return vectors


async def _index_sweep(
    space: Any,
    tenant: UUID,
    labels: tuple[UUID, ...],
    vectors: list[list[float]],
) -> dict[int, dict[str, object]]:
    """What the index returns at each setting, against the exact neighbours."""
    params = {"model": space.model, "version": space.version, "limit": CANDIDATES}

    owner = create_async_engine(settings.database_owner_url)
    async with owner.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        # Ground truth. The index is refused rather than tuned: an exact scan of 8k rows is
        # affordable here and is the only honest baseline for "what did the index miss".
        await conn.execute(text("SET LOCAL enable_indexscan = off"))
        await conn.execute(text("SET LOCAL enable_bitmapscan = off"))
        truth = []
        for vector in vectors:
            rows = await conn.execute(text(NEIGHBOURS), {**params, "embedding": str(vector)})
            truth.append({row.id for row in rows})
        await conn.rollback()
    await owner.dispose()

    # The application role, under RLS, with a tenant pinned — the conditions the deployment
    # runs in. Measured as the owner the numbers would be a different query's numbers.
    app = create_async_engine(settings.database_url)
    results: dict[int, dict[str, object]] = {}
    for ef in SWEEP:
        overlaps: list[float] = []
        times: list[float] = []
        returned = 0
        async with app.connect() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            await conn.execute(
                text("SELECT set_config('zenith.tenant_id', :t, true)"), {"t": str(tenant)}
            )
            await conn.execute(
                text("SELECT set_config('zenith.label_ids', :l, true)"),
                {"l": ",".join(str(label) for label in labels)},
            )
            await conn.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef)}"))
            for vector, exact in zip(vectors, truth, strict=True):
                fastest = None
                got: set[object] = set()
                for _ in range(REPEATS):
                    started = time.perf_counter()
                    rows = await conn.execute(
                        text(NEIGHBOURS), {**params, "embedding": str(vector)}
                    )
                    got = {row.id for row in rows}
                    elapsed = (time.perf_counter() - started) * 1000
                    fastest = elapsed if fastest is None else min(fastest, elapsed)
                returned = max(returned, len(got))
                times.append(fastest or 0.0)
                overlaps.append(len(got & exact) / len(exact))
            await conn.rollback()
        results[ef] = {
            "rows_returned": returned,
            "index_recall": round(statistics.mean(overlaps), 4),
            "worst_query": round(min(overlaps), 4),
            "median_ms": round(statistics.median(times), 2),
            "p95_ms": round(sorted(times)[int(len(times) * 0.95) - 1], 2),
        }
        print(f"  ef {ef:<4} {json.dumps(results[ef])}", flush=True)
    await app.dispose()
    return results


async def _run() -> int:
    where = await installation()
    if not where.questions:
        print("No question's document is in this corpus — nothing to measure.")
        return 1

    print(
        f"{where.space.n} embeddings, {len(where.labels)} labels, "
        f"top-{CANDIDATES}, profile {active_profile().name}"
    )
    print(f"{len(where.questions)} scorable questions\n")

    print("index, against the exact neighbours:")
    vectors = await _embed([question.question for question in where.questions])
    index = await _index_sweep(where.space, where.tenant, where.labels, vectors)

    print("\nend to end, scored as live.py scores:")
    reached: dict[str, object] = {}
    for ef in END_TO_END:
        measured = await score(where, hnsw_ef_search=ef)
        reached[str(ef)] = measured
        print(f"  ef {ef:<4} {json.dumps(measured)}", flush=True)

    report = {
        "corpus_chunks": where.space.n,
        "scored": len(where.questions),
        "candidates": CANDIDATES,
        "profile": active_profile().name,
        "index": {str(ef): value for ef, value in index.items()},
        "end_to_end": reached,
    }
    REPORT.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nWritten to {REPORT.name}")
    return 0


#: How `python -m eval` finds this sweep. Declared here rather than listed in
#: `__main__.py`, so adding a measurement is adding a file and nothing else.
COMMAND = "ef-search"
USAGE = "ef-search"


def run() -> int:
    return asyncio.run(_run())
