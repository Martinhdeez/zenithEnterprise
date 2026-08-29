# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""Where the second goes.

A search takes about 860 ms (`live-recall.json`) and the vector index scan inside it takes
**1.02 ms** (`ef-search.json`, ef=100). Those two numbers are on disk and they are three
orders of magnitude apart, so almost none of what a reader waits for is the database — and
every scaling remedy currently on the table (connection pooling, collapsing the RLS round
trips, partitioning, BM25) is aimed at the database.

That does not make those remedies wrong. It makes them remedies for a *different* axis:
they buy latency under load and at corpus sizes this installation has not reached, while the
860 ms a single user waits for today is bought somewhere else. Nobody has measured where.
`rerank-depth.json` lets it be *inferred* — five depths, a near-perfect straight line, about
94 ms per reranked candidate on a ~213 ms intercept — but an inference from a slope is not a
measurement, and buying hardware on one would be exactly the thing this repository's last
rule forbids.

So this measures it directly, stage by stage, against the running installation.

## What is timed, and why these boundaries

The stages are the ones `SearchService.search` actually has, in its order:

- `embed` — one question through TEI. The dense half cannot start without it, so it is
  serial with everything below, and it is the stage a GPU changes most.
- `session` — opening `tenant_session`: acquiring a pooled connection **and** the five
  statements that set the RLS context. Timed separately precisely because it is the cost
  the round-trip collapse proposes to remove, and that proposal has no number behind it
  either.
- `lexical`, `dense`, `exact` — the three retrieval signals, each on its own.
- `hydrate` — fetching what a citation needs for the fused set.
- `rerank` — the cross-encoder over the shortlist.

Fusion itself is not timed. It is pure Python over two lists of at most fifty UUIDs and
timing it would report scheduler noise; whatever it costs lands in `unaccounted`.

## Reading `unaccounted`

`total` is a real end-to-end `SearchService.search()` for the same question, run in the same
process. `unaccounted = total - sum(stages)` is fusion, relevance classification, dataclass
construction, and any await that yielded to the loop. **It is a check on the decomposition,
not a finding.** If it is large, the stages above are missing something and the report should
not be quoted until they are fixed.

## What this does not measure

HTTP, JSON serialisation, and the browser. This runs in-process against the service, so a
figure here is the floor of what a user experiences, not the whole of it. The gap between
`total` here and `median_ms` in `live-recall.json` is that overhead, and comparing the two is
the point of reporting both.

**Read-only.** Every query it runs is a search; nothing writes.

    docker compose exec -T api python -m eval latency [--repeats N]
"""

import asyncio
import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.core.database import tenant_session
from app.core.hardware import active as active_profile
from app.features.embeddings.client import TeiClient
from app.features.embeddings.space import SHIPPED
from app.features.retrieval.identifiers import exact
from app.features.retrieval.reranker import TeiReranker
from app.features.retrieval.search import CANDIDATES, candidates, dense, fuse, hydrate, lexical
from app.features.retrieval.service import SearchService
from eval.harness import PAGE, Installation, installation

REPORT = Path(__file__).parent / "latency.json"

#: Three passes per question. Enough for a median to be stable against one slow scheduler
#: tick, few enough that the whole sweep stays under a couple of minutes — this runs against
#: a live installation and is not worth a long lock on it.
REPEATS = 3

#: In the order they occur. `unaccounted` is derived, not timed, and stays out.
STAGES = ("embed", "session", "lexical", "dense", "exact", "hydrate", "rerank")


@dataclass(frozen=True, slots=True)
class Timings:
    """One pass over one question. Milliseconds, and every field is a real clock reading."""

    question: str
    embed: float
    session: float
    lexical: float
    dense: float
    exact: float
    hydrate: float
    rerank: float
    total: float

    @property
    def accounted(self) -> float:
        return (
            self.embed
            + self.session
            + self.lexical
            + self.dense
            + self.exact
            + self.hydrate
            + self.rerank
        )

    @property
    def unaccounted(self) -> float:
        return self.total - self.accounted


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


async def _one(where: Installation, question: str) -> Timings:
    """One question, timed at the same seams the service has.

    The stages are re-executed here rather than measured through an instrumented service on
    purpose: instrumenting `search()` would mean shipping timers in production code to answer
    a laboratory question. The cost is that `total` is a *second* execution of the same work,
    which is why it is reported beside the sum rather than assumed equal to it.
    """
    hardware = active_profile()
    profile = where.profile
    embedder = TeiClient(profile=hardware)
    reranker = TeiReranker(profile=hardware) if hardware.reranker else None

    started = time.perf_counter()
    vectors = await embedder.embed([question])
    embed_ms = _ms(started)
    embedding = vectors[0] if vectors else []

    started = time.perf_counter()
    async with tenant_session(profile.context) as session:
        # The connection and the five `set_config` statements, which is what the pooling
        # and round-trip proposals are both about.
        session_ms = _ms(started)

        started = time.perf_counter()
        lexical_scored = await lexical(session, question, CANDIDATES, None)
        lexical_ms = _ms(started)

        started = time.perf_counter()
        dense_scored = (
            await dense(
                session, embedding, SHIPPED, CANDIDATES, hardware.hnsw_ef_search, None
            )
            if embedding
            else []
        )
        dense_ms = _ms(started)

        started = time.perf_counter()
        exact_scored = await exact(session, question, documents=None)
        exact_ms = _ms(started)

        lexical_ids = [chunk_id for chunk_id, _ in lexical_scored]
        dense_ids = [chunk_id for chunk_id, _ in dense_scored]
        exact_ids = [chunk_id for chunk_id, _ in exact_scored]
        reranking = reranker is not None and hardware.rerank_candidates > 0
        ranked: list[tuple[UUID, float]] = (
            candidates(lexical_ids, dense_ids, exact_ids, hardware.rerank_candidates)
            if reranking
            else fuse(lexical_ids, dense_ids, PAGE, exact_ids)
        )

        started = time.perf_counter()
        hits = await hydrate(
            session, ranked, lexical_ids, dense_ids, dict(lexical_scored), dict(dense_scored)
        )
        hydrate_ms = _ms(started)

    rerank_ms = 0.0
    if reranking and hits and reranker is not None:
        started = time.perf_counter()
        await reranker.rank(question, [hit.text for hit in hits])
        rerank_ms = _ms(started)

    started = time.perf_counter()
    await SearchService(profile).search(question, limit=PAGE)
    total_ms = _ms(started)

    return Timings(
        question=question,
        embed=embed_ms,
        session=session_ms,
        lexical=lexical_ms,
        dense=dense_ms,
        exact=exact_ms,
        hydrate=hydrate_ms,
        rerank=rerank_ms,
        total=total_ms,
    )


def _summarise(passes: list[Timings]) -> dict[str, object]:
    """Medians, and each stage's share of the median total.

    Medians rather than means throughout: one request that lost the CPU to the ingestion
    worker would drag a mean by hundreds of milliseconds and say nothing about the pipeline.
    `vps-contention.json` is what that looks like when it happens.
    """
    total = statistics.median([p.total for p in passes])
    stages: dict[str, object] = {}
    for stage in STAGES:
        values = [getattr(p, stage) for p in passes]
        stages[stage] = {
            "median_ms": round(statistics.median(values), 2),
            "p95_ms": round(sorted(values)[min(len(values) - 1, int(len(values) * 0.95))], 2),
            "share": round(statistics.median(values) / total, 4) if total else None,
        }
    unaccounted = statistics.median([p.unaccounted for p in passes])
    return {
        "passes": len(passes),
        "total_median_ms": round(total, 2),
        "total_p95_ms": round(sorted([p.total for p in passes])[int(len(passes) * 0.95) - 1], 2),
        "stages": stages,
        "unaccounted_median_ms": round(unaccounted, 2),
        "unaccounted_share": round(unaccounted / total, 4) if total else None,
    }


async def _run(repeats: int) -> int:
    where = await installation()
    if not where.questions:
        print("No question's document is in this corpus — nothing to measure.")
        return 1

    hardware = active_profile()
    questions = [q.question for q in where.questions]
    print(
        f"{where.space.n} embeddings, {len(questions)} questions, {repeats} passes each\n"
        f"profile {hardware.name}  rerank_candidates {hardware.rerank_candidates}  "
        f"ef_search {hardware.hnsw_ef_search}\n"
    )

    passes: list[Timings] = []
    for index, question in enumerate(questions, start=1):
        for _ in range(repeats):
            passes.append(await _one(where, question))
        last = passes[-1]
        print(
            f"  {index:>2}/{len(questions)}  {last.total:7.1f} ms total   "
            f"embed {last.embed:6.1f}  rerank {last.rerank:6.1f}  "
            f"db {last.session + last.lexical + last.dense + last.exact + last.hydrate:6.1f}",
            flush=True,
        )

    summary = _summarise(passes)
    REPORT.write_text(
        json.dumps(
            {
                "profile": hardware.name,
                "rerank_candidates": hardware.rerank_candidates,
                "hnsw_ef_search": hardware.hnsw_ef_search,
                "candidates": CANDIDATES,
                "graph_embeddings": where.space.n,
                "questions": len(questions),
                "repeats": repeats,
                **summary,
                "by_question": [
                    {
                        "question": question,
                        **{
                            stage: round(
                                statistics.median(
                                    [getattr(p, stage) for p in passes if p.question == question]
                                ),
                                2,
                            )
                            for stage in (*STAGES, "total")
                        },
                    }
                    for question in questions
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )

    print(f"\n  {'stage':<12}{'median ms':>12}{'share':>10}")
    for stage in STAGES:
        row = summary["stages"][stage]  # type: ignore[index]
        share = row["share"]  # type: ignore[index]
        print(f"  {stage:<12}{row['median_ms']:>12}{(f'{share:.1%}' if share else '—'):>10}")
    print(f"  {'unaccounted':<12}{summary['unaccounted_median_ms']:>12}")
    print(f"\n  total median {summary['total_median_ms']} ms")
    print(f"\nWritten to {REPORT.name}")
    return 0


#: How `python -m eval` finds this sweep. Declared here rather than listed in
#: `__main__.py`, so adding a measurement is adding a file and nothing else.
COMMAND = "latency"
USAGE = "latency [--repeats N]"


def run(repeats: int = REPEATS) -> int:
    return asyncio.run(_run(repeats))


def cli(argv: list[str]) -> int:
    """`--repeats N`, or the default."""
    if "--repeats" in argv:
        return run(int(argv[argv.index("--repeats") + 1]))
    return run()
