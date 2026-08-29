# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""What the vector index costs per passage, and what shrinking it costs in recall.

The HNSW index is the structural ceiling on how much corpus one installation can hold.
Measured here rather than assumed: the production index was ~8,940 bytes per vector for
`vector(1024)` at `m=16, ef_construction=64`, and HNSW wants to be resident. At this corpus's
measured passages-per-document that is thousands of gigabytes at a million documents, which
is the number that decides whether the product runs on an enterprise server or does not.

**This sweep is what migration 0025 was decided on, and it still runs after it.** The
installation's index is now `halfvec_cosine_ops` on `embedding_half`, so the `fp16` row below
is the deployed representation and `fp32` is the baseline it replaced. The fp32 and binary
arms are still built here, on scratch copies, because the comparison is the report.

pgvector 0.8 offers two smaller representations, and this sweeps both against the fp32 index
they would replace:

- **`halfvec`** — fp16, half the bytes per component, no rescoring step.
- **`bit` + `bit_hamming_ops`** — one bit per component, retrieved by Hamming distance and
  then *rescored* against the fp32 vectors in the heap. The rescore width is the knob.

## Three things a naive probe gets wrong, and what this does instead

**1. The queries have to be questions.** Sampling stored chunk embeddings and using them as
queries measures passage→passage similarity. Retrieval is question→passage, which is
asymmetric and a different distribution; a quantisation that preserves one need not preserve
the other. Every query here is a real question from `questions.toml`, embedded through the
same `TeiClient` the product uses.

**2. Index recall is not the product.** What a reader sees is the page after fusion and the
cross-encoder, and both can repair — or fail to repair — what the index lost. So both are
measured: index recall against exact cosine, *and* end-to-end Recall@8 through the real
retrieval functions, scored by `eval/live.py`'s credit rule so the number can be read beside
`live-recall.json`. Inventing a second credit rule here would make the reports incomparable,
which is the one thing they exist to be.

**3. 13,549 vectors is not the regime in question.** Binary quantisation degrades as N grows:
more vectors means more near-collisions in a 1024-bit Hamming space, and a result at 13.5k
says very little about 300M. 300M cannot be built here. The *trend* can be measured, so the
sweep runs at several subset sizes and reports whether binary's gap to exact is flat or
widening, with a fitted slope. A widening gap is the finding that matters, and it is worth
more than a favourable headline number.

## What runs where

Everything this builds lives in one schema, `zenith_quantisation`, created at the start and
dropped in a `finally` so a crash does not leave it behind. **`chunk_embeddings` is read and
never written**: the subsets are copies, the indexes are built on the copies, and no
production table gains a column, an index or a row. Nothing here is `SECURITY DEFINER` and
nothing needs to be.

The copies carry no RLS policy and no join to `chunks`, so a dense latency here is not
comparable to a dense latency in `latency.json`. It is comparable to the other variants in
this report, which is what the report is for.

**Read-only with respect to the corpus.** The only writes are to the scratch schema.

    docker compose exec -T api python -m eval quantisation [--subsets 2000,5000,10000]
"""

import asyncio
import json
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.core.config import settings
from app.core.database import tenant_session
from app.core.hardware import active as active_profile
from app.features.embeddings.client import MODEL, VERSION, TeiClient
from app.features.retrieval.identifiers import exact
from app.features.retrieval.reranker import TeiReranker
from app.features.retrieval.search import CANDIDATES, Hit, candidates, fuse, hydrate, lexical
from eval.harness import PAGE, Installation, installation
from eval.live import FILENAMES

REPORT = Path(__file__).parent / "quantisation.json"

#: One schema, one name, dropped in a `finally`. The F18 investigation left two undeclared
#: `SECURITY DEFINER` functions in this database for weeks; a scratch object that is not
#: trivially greppable and trivially droppable is how that happens.
SCHEMA = "zenith_quantisation"

#: The production index's parameters, so the fp32 baseline measured here is the index the
#: installation actually runs and not a differently-tuned cousin.
M = 16
EF_CONSTRUCTION = 64

#: Subset sizes. The largest is filled in at runtime with the whole corpus, because the
#: point of the largest point is that it is the real one. Each subset is a prefix of the
#: same deterministic ordering, so they nest: the 2k rows are inside the 5k rows.
SUBSETS = (2000, 5000, 10000)

#: How many neighbours the truth set holds. `CANDIDATES` is what the dense half asks for, so
#: recall at that depth is the number the product's behaviour depends on. Recall@10 is
#: carried alongside it because it is the depth a reader's page is drawn from and the depth
#: most published quantisation figures use.
DEPTHS = (10, CANDIDATES)

#: Rescore widths for the binary variant: how many Hamming neighbours are re-ordered by exact
#: cosine before the top-k is taken. Below `CANDIDATES` the width itself caps recall@50 —
#: reported rather than hidden, because a narrow rescore is a real deployment choice and its
#: ceiling is part of the cost. Above the profile's `hnsw.ef_search` the width is unreachable
#: without widening the graph walk to match, so `_index_recall` raises `ef_search` to the
#: width; the reasoning is there.
RESCORE = (20, 50, 100, 200, 400)

#: Repeats per query per variant. The fastest is kept: the question is what the scan costs,
#: not what the container happened to be doing.
REPEATS = 5

#: Corpus sizes to project to, in passages. A million documents at this corpus's measured
#: passages-per-document is the middle of this range.
PROJECTIONS = (1_000_000, 10_000_000, 50_000_000, 100_000_000, 300_000_000)

#: The same projection in the unit a customer counts in. Passages are the thing indexed;
#: documents are the thing bought.
DOCUMENT_PROJECTIONS = (100_000, 1_000_000, 10_000_000)

#: A second, lower passages-per-document assumption, quoted beside the measured one. This
#: corpus is long legal and standards PDFs and its average is high; a corporate mix of memos,
#: contracts and slide decks is shorter. Neither number is hidden inside a constant used for
#: arithmetic — both are reported and the projection is given for both.
CORPORATE_PASSAGES_PER_DOCUMENT = 80.0


@dataclass(frozen=True, slots=True)
class Variant:
    """One index, and the query that uses it."""

    #: Report key: `fp32`, `fp16`, `binary_r100`.
    key: str
    #: `vector`, `halfvec` or `bit` — which stored column the index covers.
    column: str
    opclass: str
    #: Hamming neighbours re-ordered by exact cosine. `None` for the un-rescored variants.
    rescore: int | None = None

    @property
    def index(self) -> str:
        return f"ix_{self.column}"


#: One index per representation; the rescore widths share the single `bit` index, because the
#: width is a query-time choice and building three identical indexes would only report noise.
INDEXES = (
    Variant(key="fp32", column="embedding", opclass="vector_cosine_ops"),
    Variant(key="fp16", column="embedding_half", opclass="halfvec_cosine_ops"),
    Variant(key="binary", column="embedding_bit", opclass="bit_hamming_ops"),
)

VARIANTS = (
    Variant(key="fp32", column="embedding", opclass="vector_cosine_ops"),
    Variant(key="fp16", column="embedding_half", opclass="halfvec_cosine_ops"),
    *(
        Variant(
            key=f"binary_r{width}", column="embedding_bit", opclass="bit_hamming_ops", rescore=width
        )
        for width in RESCORE
    ),
)


def _table(size: int) -> str:
    return f"{SCHEMA}.subset_{size}"


#: The measurement primitives below take the table they read and the suffix that keeps index
#: names unique, rather than deriving both from a subset size. `eval/scale.py` measures the
#: same variants over a synthetic corpus that has no subset size, and a second copy of the
#: recall rule is the thing `eval/harness.py` argues against by name: two reports computed by
#: two implementations of one rule cannot be read beside each other, which is the only use
#: either of them has.


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


async def _build_subset(conn: AsyncConnection, size: int, space: Any) -> dict[str, object]:
    """Copy `size` embeddings into the scratch schema, in all three representations.

    Ordered by `md5(chunk_id)` rather than randomly, so the subsets are reproducible between
    runs and nested within each other: the trend across sizes is then the same vectors plus
    more, rather than four unrelated samples.
    """
    table = _table(size)
    await conn.execute(
        text(
            f"CREATE TABLE {table} AS "
            "SELECT chunk_id, embedding, "
            "embedding::halfvec(1024) AS embedding_half, "
            "binary_quantize(embedding)::bit(1024) AS embedding_bit "
            "FROM chunk_embeddings "
            "WHERE embedding_model = :model AND embedding_version = :version "
            "ORDER BY md5(chunk_id::text) LIMIT :n"
        ),
        {"model": space.model, "version": space.version, "n": size},
    )
    await conn.execute(text(f"ANALYZE {table}"))
    rows = (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()

    built: dict[str, object] = {}
    for variant in INDEXES:
        started = time.perf_counter()
        await conn.execute(
            text(
                f"CREATE INDEX {variant.index}_{size} ON {table} "
                f"USING hnsw ({variant.column} {variant.opclass}) "
                f"WITH (m = {M}, ef_construction = {EF_CONSTRUCTION})"
            )
        )
        build_ms = (time.perf_counter() - started) * 1000
        size_bytes = (
            await conn.execute(
                text("SELECT pg_relation_size(:name)"),
                {"name": f"{SCHEMA}.{variant.index}_{size}"},
            )
        ).scalar_one()
        built[variant.key] = {
            "index_bytes": int(size_bytes),
            "bytes_per_vector": round(int(size_bytes) / rows, 1),
            "build_ms": round(build_ms),
            "build_vectors_per_second": round(rows / (build_ms / 1000), 1),
        }
        print(
            f"    {variant.key:<8} {int(size_bytes) / rows:8.1f} B/vec  "
            f"build {build_ms / 1000:6.2f} s",
            flush=True,
        )
    return {"rows": int(rows), "indexes": built}


def _index_statement(table: str, variant: Variant) -> str:
    """The statement the arm's index leg runs.

    Shared with `_plan` so the plan recorded in the report is the plan of the query that was
    actually timed, rather than of a near-copy that drifted away from it.
    """
    if variant.rescore is None:
        cast = "vector" if variant.column == "embedding" else "halfvec(1024)"
        return (
            f"SELECT chunk_id FROM {table} "
            f"ORDER BY {variant.column} <=> CAST(:q AS {cast}) LIMIT :k"
        )
    return (
        f"SELECT chunk_id FROM {table} "
        "ORDER BY embedding_bit <~> binary_quantize(CAST(:q AS vector))::bit(1024) LIMIT :k"
    )


def _truth_statement(table: str) -> str:
    return f"SELECT chunk_id FROM {table} ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"


async def _plan(conn: AsyncConnection, statement: str, params: dict[str, object]) -> str:
    """The node types Postgres chose, joined into one line.

    Recorded rather than trusted. The two settings the ground truth depends on are planner
    hints rather than commands — `enable_indexscan = off` raises a cost, it does not forbid a
    plan — and a hint that silently failed, or one that outlived the statement it was meant
    for, turns a recall number into a comparison of something against itself. That is not
    hypothetical: it is the defect this file was carrying. The plan is in the report so that
    "the truth was a sequential scan and each arm used its own HNSW index" is a fact a reader
    can check instead of a sentence a docstring asserts.
    """
    rows = await conn.execute(text(f"EXPLAIN (COSTS off) {statement}"), params)
    # Truncated per line: a sort key on a vector distance prints the whole 1024-component
    # literal, which would be most of this report by weight and none of it by content.
    return " | ".join(line.strip()[:60] for (line,) in rows.fetchall() if line and line.strip())


async def _truth(
    conn: AsyncConnection, table: str, vectors: list[list[float]]
) -> tuple[list[list[UUID]], str]:
    """Exact nearest neighbours, with every index refused. Returns the ids and the plan.

    `enable_indexscan = off` is not belt and braces: the only honest ground truth for "what
    did the index miss" is a scan that did not use an index. Comparing one approximation
    against another would report the difference between two errors.

    **Both settings are `SET LOCAL`, so they last to the end of the transaction and not to the
    end of this function.** Turning them back on is `_index_recall`'s first act, and the
    returned plan is half of the evidence that the pair worked; each arm's `plan_uses_index`
    is the other half.
    """
    await conn.execute(text("SET LOCAL enable_indexscan = off"))
    await conn.execute(text("SET LOCAL enable_bitmapscan = off"))
    await conn.execute(text("SET LOCAL enable_seqscan = on"))
    statement = _truth_statement(table)
    plan = await _plan(conn, statement, {"q": str(vectors[0]), "k": max(DEPTHS)})
    truth: list[list[UUID]] = []
    for vector in vectors:
        rows = await conn.execute(text(statement), {"q": str(vector), "k": max(DEPTHS)})
        truth.append([row.chunk_id for row in rows])
    return truth, plan


async def _probe(
    conn: AsyncConnection,
    table: str,
    variant: Variant,
    vector: list[float],
    limit: int,
) -> tuple[list[UUID], float, float]:
    """One approximate query. Returns the ids, the index time and the rescore time.

    The two halves of the binary variant are timed apart because they are different costs
    with different scaling: the Hamming walk stays in the small index, and the rescore is a
    heap read of `rescore` fp32 vectors — random I/O against data quantisation did *not*
    shrink. Reporting one number would hide the half that grows.
    """
    statement = _index_statement(table, variant)
    if variant.rescore is None:
        started = time.perf_counter()
        rows = await conn.execute(text(statement), {"q": str(vector), "k": limit})
        ids = [row.chunk_id for row in rows]
        return ids, (time.perf_counter() - started) * 1000, 0.0

    started = time.perf_counter()
    rows = await conn.execute(text(statement), {"q": str(vector), "k": variant.rescore})
    shortlist = [row.chunk_id for row in rows]
    index_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    rows = await conn.execute(
        text(
            f"SELECT chunk_id FROM {table} WHERE chunk_id = ANY(:ids) "
            "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
        ),
        {"ids": [str(chunk_id) for chunk_id in shortlist], "q": str(vector), "k": limit},
    )
    ids = [row.chunk_id for row in rows]
    return ids, index_ms, (time.perf_counter() - started) * 1000


async def _index_recall(
    conn: AsyncConnection,
    table: str,
    vectors: list[list[float]],
    truth: list[list[UUID]],
    ef_search: int,
    suffix: str,
) -> dict[str, dict[str, object]]:
    """Every variant against the exact neighbours, at every depth.

    `suffix` is what keeps the index names unique across subset sizes, and it is needed here
    to name the index each arm is supposed to be using so the recorded plan can be checked
    against it.
    """
    # `_truth` ran `enable_indexscan = off` on this same connection, and `SET LOCAL` lasts to
    # the end of the transaction rather than to the end of the statement. Without these two
    # lines every arm is answered by a sequential scan over the subset copy, which is exact —
    # so fp32 and fp16 score a perfect recall against a truth computed the same way, and the
    # sweep reports that halving the precision is free when it has measured nothing at all.
    # That is not a hypothetical: the report committed before this fix said exactly that.
    # `plan_uses_index` below is what proves it is not happening now.
    #
    # `enable_seqscan = off` is the other half, and it is not paranoia either: with the
    # settings merely restored, the planner still chose a sequential scan for some arms on
    # these scratch copies — fp16 at 13,549 rows among them, at 20 ms against fp32's 0.9 ms
    # through its index. An arm that opts out of its own index measures nothing, in exactly
    # the way the ground truth measures nothing when it does not. Both are hints rather than
    # commands, which is why the plan is recorded and not assumed.
    await conn.execute(text("SET LOCAL enable_indexscan = on"))
    await conn.execute(text("SET LOCAL enable_bitmapscan = on"))
    await conn.execute(text("SET LOCAL enable_seqscan = off"))
    measured: dict[str, dict[str, object]] = {}

    for variant in VARIANTS:
        index_name = f"{variant.index}_{suffix}"
        # An HNSW scan returns at most `ef_search` rows however large the `LIMIT` is —
        # measured, at ef_search 100: LIMIT 200 and LIMIT 400 both return 100. So a rescore
        # width above the profile's ef_search is unreachable unless the walk is widened to
        # match it, and pinning ef_search here would have made `binary_r200` and
        # `binary_r400` silent duplicates of `binary_r100`. The width is raised rather than
        # the report faked: a deployment choosing a 400-wide rescore has to raise ef_search
        # too, and the wider graph walk is part of what that width costs.
        ef_used = max(int(ef_search), variant.rescore or 0)
        await conn.execute(text(f"SET LOCAL hnsw.ef_search = {ef_used}"))
        plan = await _plan(
            conn,
            _index_statement(table, variant),
            {"q": str(vectors[0]), "k": variant.rescore or max(DEPTHS)},
        )
        overlaps: dict[int, list[float]] = {depth: [] for depth in DEPTHS}
        index_times: list[float] = []
        rescore_times: list[float] = []

        for vector, exact_ids in zip(vectors, truth, strict=True):
            got: list[UUID] = []
            fastest_index: float | None = None
            fastest_rescore: float | None = None
            for _ in range(REPEATS):
                got, index_ms, rescore_ms = await _probe(conn, table, variant, vector, max(DEPTHS))
                fastest_index = index_ms if fastest_index is None else min(fastest_index, index_ms)
                fastest_rescore = (
                    rescore_ms if fastest_rescore is None else min(fastest_rescore, rescore_ms)
                )
            index_times.append(fastest_index or 0.0)
            rescore_times.append(fastest_rescore or 0.0)
            for depth in DEPTHS:
                wanted = set(exact_ids[:depth])
                overlaps[depth].append(len(set(got[:depth]) & wanted) / len(wanted))

        measured[variant.key] = {
            "rescore_width": variant.rescore,
            "ef_search": ef_used,
            "plan": plan,
            "plan_uses_index": index_name in plan,
            **{
                f"index_recall_at_{depth}": round(statistics.mean(overlaps[depth]), 4)
                for depth in DEPTHS
            },
            **{f"worst_query_at_{depth}": round(min(overlaps[depth]), 4) for depth in DEPTHS},
            "index_median_ms": round(statistics.median(index_times), 3),
            "index_p95_ms": round(_percentile(index_times, 0.95), 3),
            "rescore_median_ms": round(statistics.median(rescore_times), 3),
            "rescore_p95_ms": round(_percentile(rescore_times, 0.95), 3),
        }
        row = measured[variant.key]
        print(
            f"    {variant.key:<12} r@10 {row['index_recall_at_10']:.4f}  "
            f"r@{CANDIDATES} {row[f'index_recall_at_{CANDIDATES}']:.4f}  "
            f"{row['index_median_ms']:.2f} + {row['rescore_median_ms']:.2f} ms  "
            f"{'index' if row['plan_uses_index'] else 'NO INDEX'}",
            flush=True,
        )
    return measured


#: What an alternative dense half has to look like to be measured end to end: given a
#: scratch connection and a query embedding, return the candidate chunk ids it proposes.
DenseStage = Callable[[AsyncConnection, list[float]], Awaitable[list[UUID]]]


async def end_to_end(
    where: Installation,
    dense_stage: DenseStage,
    ef_search: int,
) -> dict[str, object]:
    """The real pipeline with only the dense half swapped for the one under test.

    Parameterised by the dense stage rather than by a quantisation variant, because
    `eval/coarse.py` substitutes a two-stage coarse-then-fine retrieval that is not a variant
    of anything here. Two copies of this function would be two credit rules, two orderings
    and two definitions of what reached the page — and a number from one could not be read
    beside a number from the other, which is the only use either has.

    `lexical`, `exact`, `candidates`, `fuse`, `hydrate` and the cross-encoder are the
    product's own functions, called in the product's order. Reimplementing them to measure
    them would measure the reimplementation — the same argument `latency.py` makes for
    decomposing rather than instrumenting.

    Scored by `eval/live.py`'s credit rule: a passage counts when its `(document, page)` is
    one the question records.
    """
    hardware = active_profile()
    embedder = TeiClient(profile=hardware)
    reranker = TeiReranker(profile=hardware) if hardware.reranker else None
    owner = create_async_engine(settings.database_owner_url)

    ranks: list[tuple[bool, int | None]] = []
    times: list[float] = []
    dense_times: list[float] = []

    try:
        async with owner.connect() as scratch:
            await scratch.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))
            for question in where.questions:
                wanted = {
                    (FILENAMES[source.document], page)
                    for source in question.sources
                    for page in source.pages
                }
                started = time.perf_counter()

                embedding = await embedder.embed_query(question.question)
                dense_started = time.perf_counter()
                dense_ids = await dense_stage(scratch, embedding)
                dense_times.append((time.perf_counter() - dense_started) * 1000)

                async with tenant_session(where.profile.context) as session:
                    lexical_scored = await lexical(session, question.question, CANDIDATES, None)
                    exact_scored = await exact(session, question.question, documents=None)
                    lexical_ids = [chunk_id for chunk_id, _ in lexical_scored]
                    exact_ids = [chunk_id for chunk_id, _ in exact_scored]
                    reranking = reranker is not None and hardware.rerank_candidates > 0
                    ranked = (
                        candidates(lexical_ids, dense_ids, exact_ids, hardware.rerank_candidates)
                        if reranking
                        else fuse(lexical_ids, dense_ids, PAGE, exact_ids)
                    )
                    hits: list[Hit] = await hydrate(
                        session, ranked, lexical_ids, dense_ids, dict(lexical_scored), {}
                    )

                if reranking and hits and reranker is not None:
                    scored = await reranker.rank(question.question, [hit.text for hit in hits])
                    hits = [
                        replace(hits[item.index], rerank_score=item.score) for item in scored[:PAGE]
                    ]
                else:
                    hits = hits[:PAGE]

                times.append((time.perf_counter() - started) * 1000)
                rank = next(
                    (
                        position + 1
                        for position, hit in enumerate(hits)
                        if (hit.filename, hit.page_num) in wanted
                    ),
                    None,
                )
                ranks.append((question.counts_towards_headline, rank))
            await scratch.rollback()
    finally:
        await owner.dispose()

    headline = [rank for counts, rank in ranks if counts]
    return {
        "headline_recall_at_8": round(sum(r is not None for r in headline) / len(headline), 4),
        "headline_scored": len(headline),
        "recall_at_8_all": round(sum(r is not None for _, r in ranks) / len(ranks), 4),
        "recall_at_1_all": round(sum(r == 1 for _, r in ranks) / len(ranks), 4),
        "dense_median_ms": round(statistics.median(dense_times), 3),
        "dense_p95_ms": round(_percentile(dense_times, 0.95), 3),
        "search_median_ms": round(statistics.median(times)),
        "search_p95_ms": round(_percentile(times, 0.95)),
    }


def _trend(sizes: list[int], by_size: dict[int, dict[str, Any]]) -> dict[str, object]:
    """Does the gap to exact widen with N, and how fast.

    A least-squares slope of `1 - recall` against `log10(N)`, which is the shape the question
    is asked in: nobody can build 300M vectors, but a gap that grows by a fixed amount per
    decade is a gap that can be extrapolated by decades. Reported with the raw per-size gaps
    beside it so the fit can be disbelieved.

    Both depths are fitted, because they answer different questions and one of them is
    contaminated. Recall@50 for a rescore width below 50 is capped by the width itself — a
    20-wide rescore can return at most 20 of 50 — so its gap is arithmetic rather than
    quantisation error. Recall@10 is uncapped at every width here and is the depth a reader's
    page is drawn from.
    """
    import math

    trends: dict[str, object] = {}
    xs = [math.log10(n) for n in sizes]
    for variant in VARIANTS:
        fits: dict[str, object] = {"rescore_width": variant.rescore}
        for depth in DEPTHS:
            key = f"index_recall_at_{depth}"
            gaps = [1.0 - float(by_size[n]["index"][variant.key][key]) for n in sizes]
            mean_x, mean_y = statistics.mean(xs), statistics.mean(gaps)
            denominator = sum((x - mean_x) ** 2 for x in xs)
            slope = (
                sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, gaps, strict=True))
                / denominator
                if denominator
                else 0.0
            )
            fits[f"at_{depth}"] = {
                "gap_by_size": {str(n): round(gap, 4) for n, gap in zip(sizes, gaps, strict=True)},
                "gap_per_decade": round(slope, 4),
                "widening": slope > 0.005,
                # Capped by the rescore width rather than by quantisation, and said so here
                # rather than left for a reader to notice.
                "width_capped": variant.rescore is not None and variant.rescore < depth,
                # What the fitted line says at the sizes nobody can build. An extrapolation,
                # and labelled one: it assumes the trend stays log-linear, which is exactly
                # the assumption a test at scale would exist to check.
                "extrapolated_gap": {
                    str(n): round(
                        min(1.0, max(0.0, gaps[-1] + slope * (math.log10(n) - xs[-1]))), 4
                    )
                    for n in PROJECTIONS
                },
            }
        trends[variant.key] = fits
    return trends


def _projection(
    bytes_per_vector: dict[str, float], passages_per_document: float
) -> dict[str, object]:
    """Index size at corpus sizes this installation will never reach on this machine.

    Straight arithmetic on the measured bytes per vector: HNSW's per-vector cost is `m` and
    the dimension, neither of which changes with N, so the linear projection is sound for
    *size*. It says nothing about build time or about recall, both of which do change with N
    and are measured separately above.
    """
    return {
        "passages_per_document": passages_per_document,
        "documents_at_each_size": {str(n): round(n / passages_per_document) for n in PROJECTIONS},
        "index_gib_by_passages": {
            key: {str(n): round(value * n / 1024**3, 1) for n in PROJECTIONS}
            for key, value in bytes_per_vector.items()
        },
        "index_gib_by_documents": {
            key: {
                str(d): round(value * d * passages_per_document / 1024**3, 1)
                for d in DOCUMENT_PROJECTIONS
            }
            for key, value in bytes_per_vector.items()
        },
    }


async def _run(subsets: tuple[int, ...]) -> int:
    where = await installation()
    if not where.questions:
        print("No question's document is in this corpus — nothing to measure.")
        return 1

    hardware = active_profile()
    ef_search = hardware.hnsw_ef_search
    owner = create_async_engine(settings.database_owner_url)

    questions = [question.question for question in where.questions]
    print(
        f"{where.space.n} embeddings, {len(questions)} scorable questions, "
        f"profile {hardware.name}, ef_search {ef_search}"
    )

    embedder = TeiClient(profile=hardware)
    vectors = [await embedder.embed_query(question) for question in questions]
    print(f"{len(vectors)} questions embedded through {MODEL} {VERSION}\n")

    sizes = sorted({*subsets, int(where.space.n)})
    by_size: dict[int, dict[str, Any]] = {}
    documents = chunks = production_index_bytes = 0

    try:
        async with owner.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
            await conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
            # The default 64 MB makes HNSW construction spill and crawl, which would report
            # a build time for the memory setting rather than for the representation.
            await conn.execute(text("SET maintenance_work_mem = '512MB'"))

            counted = (
                await conn.execute(text("SELECT count(DISTINCT document_id), count(*) FROM chunks"))
            ).one()
            documents, chunks = int(counted[0]), int(counted[1])
            # `to_regclass` rather than a bare name: the production index is
            # `ix_chunk_embeddings_hnsw_half` from migration 0025 and was
            # `ix_chunk_embeddings_hnsw` before it, and this sweep has to run either side of
            # that migration — it is what decides whether to apply it. A hard-coded name
            # would make the report fail on exactly the installation it is measuring.
            production_index_bytes = int(
                (
                    await conn.execute(
                        text(
                            "SELECT coalesce(pg_relation_size(to_regclass("
                            "'ix_chunk_embeddings_hnsw_half')), "
                            "pg_relation_size(to_regclass('ix_chunk_embeddings_hnsw')), 0)"
                        )
                    )
                ).scalar_one()
            )

            for size in sizes:
                print(f"  {size} vectors:")
                built = await _build_subset(conn, size, where.space)
                truth, truth_plan = await _truth(conn, _table(size), vectors)
                print(f"    truth plan: {truth_plan}", flush=True)
                recall = await _index_recall(
                    conn, _table(size), vectors, truth, ef_search, str(size)
                )
                by_size[size] = {
                    **built,
                    "truth_plan": truth_plan,
                    "truth_is_sequential": "Seq Scan" in truth_plan,
                    "index": recall,
                }

        full = sizes[-1]
        print("\n  end to end, scored as live.py scores, at the full corpus:")
        reached: dict[str, object] = {}
        for variant in VARIANTS:

            def probe_variant(
                scratch: AsyncConnection, embedding: list[float], v: Variant = variant
            ) -> Awaitable[list[UUID]]:
                async def _go() -> list[UUID]:
                    # Set here rather than inside `end_to_end`, which `eval/coarse.py` also
                    # calls: the dense stage is this file's to constrain, and coarse's is
                    # not. `SET LOCAL` persists for the connection's transaction, so the
                    # repeat on every question costs nothing and keeps the setting beside
                    # the query it exists for.
                    await scratch.execute(text("SET LOCAL enable_seqscan = off"))
                    ids, _, _ = await _probe(scratch, _table(full), v, embedding, CANDIDATES)
                    return ids

                return _go()

            # Same widening as `_index_recall`, for the same reason: at ef_search 100 a
            # 400-wide shortlist is 100 rows long and the width is not being measured.
            measured = await end_to_end(where, probe_variant, max(ef_search, variant.rescore or 0))
            reached[variant.key] = measured
            print(f"    {variant.key:<12} {json.dumps(measured)}", flush=True)

        # Keyed by representation rather than by variant: the five rescore widths share one
        # `bit` index, and repeating its size five times would read as five measurements.
        bytes_per_vector = {
            index.key: float(by_size[full]["indexes"][index.key]["bytes_per_vector"])
            for index in INDEXES
        }
        baseline = bytes_per_vector["fp32"]
        passages_per_document = round(chunks / documents, 1)
        report = {
            "corpus": {
                "documents": documents,
                "chunks": chunks,
                "passages_per_document": passages_per_document,
                "production_index_bytes": production_index_bytes,
                "production_bytes_per_vector": round(production_index_bytes / chunks, 1),
                "freshly_built_bytes_per_vector": baseline,
                "production_note": (
                    "`production_bytes_per_vector` is the live index, which is fp16 since "
                    "migration 0025 and therefore comparable to the `fp16` arm below rather "
                    "than to `freshly_built_bytes_per_vector` — that is the fp32 baseline "
                    "every ratio here is taken against. A live index carrying ingestion "
                    "churn reads larger per vector than a freshly built one of the same "
                    "representation, which is why every ratio below is fresh-against-fresh."
                ),
            },
            "bytes_per_vector": bytes_per_vector,
            "smaller_than_fp32": {
                key: round(baseline / value, 2) for key, value in bytes_per_vector.items()
            },
            "questions": len(questions),
            "profile": hardware.name,
            "hnsw": {"m": M, "ef_construction": EF_CONSTRUCTION, "ef_search": ef_search},
            "candidates": CANDIDATES,
            "repeats": REPEATS,
            "planner": {
                "truth": (
                    "Exact fp32 cosine over the subset copy, with enable_indexscan and "
                    "enable_bitmapscan off. Ground truth for every arm at that size."
                ),
                "arms": (
                    "Both settings are restored, and enable_seqscan turned off, before any "
                    "arm is measured. SET LOCAL lasts to the end of the transaction, not "
                    "the end of the statement, and the "
                    "report published before this note was written did not restore them: "
                    "every arm was answered by a sequential scan, so the fp32 and fp16 "
                    "index-recall rows compared exact retrieval against itself and were "
                    "necessarily 1.0000. The binary rows were unaffected in kind, because "
                    "Hamming distance over sign bits differs from cosine over full vectors "
                    "whichever scan reads them. enable_seqscan is off because restoring the "
                    "settings alone was not enough: the planner still chose a sequential "
                    "scan for some arms on these scratch copies, which measures nothing for "
                    "the same reason."
                ),
                "evidence": (
                    "`truth_is_sequential` under each size, and `plan_uses_index` on each "
                    "arm, are read off EXPLAIN of the statement that was timed. They make "
                    "the claim checkable rather than asserted."
                ),
            },
            "sizes": sizes,
            "by_size": {str(size): value for size, value in by_size.items()},
            "end_to_end": {
                "measured_at_vectors": full,
                "note": (
                    "End to end runs at the full corpus only. A subset that excludes the "
                    "passage a question is credited for would score a missing document as a "
                    "quantisation failure, which is a different measurement."
                ),
                "resolution_note": (
                    "The headline is scored over a small number of questions, so it cannot "
                    "resolve a difference finer than one question. Read 'no change' as 'no "
                    "change larger than one question in headline_scored', not as proof of "
                    "equality."
                ),
                "dense_timing_note": (
                    "One pass per question, in variant order, so these dense figures carry "
                    "the cache state the previous variant left. The comparable dense "
                    "latencies are the fastest-of-N figures under by_size."
                ),
                "variants": reached,
            },
            "caveats": [
                "Latencies are measured against scratch copies that hold no RLS policy and "
                "no join to chunks, and are taken while the scratch schema is competing for "
                "128 MB of shared_buffers with the production corpus. They are comparable to "
                "each other and not to ef-search.json or latency.json.",
                "pgvector does not raise the effective ef_search to the query's LIMIT. An "
                "HNSW scan returns at most ef_search rows: measured at ef_search 100, both "
                "LIMIT 200 and LIMIT 400 return 100. So this sweep raises ef_search to the "
                "rescore width for the arms that need it — see each arm's ef_search — and "
                "the wider graph walk is part of what a wide rescore costs, not the rescore "
                "alone. An earlier version of this caveat asserted the opposite; it was "
                "written from a run in which no arm used an index and the behaviour could "
                "therefore not have been observed.",
                "Binary quantisation shrinks the index, not the table. Rescoring reads the "
                "fp32 vectors from the heap, so the storage saving is in what has to stay "
                "resident, which is the constraint this measurement is about.",
                "Nothing here tests a corpus in a different language mix, and nothing tests "
                "index build time at the scale that decides the question.",
            ],
            "trend": _trend(sizes, by_size),
            "projection": {
                "this_corpus": _projection(bytes_per_vector, passages_per_document),
                "corporate_mix": _projection(bytes_per_vector, CORPORATE_PASSAGES_PER_DOCUMENT),
            },
        }
        REPORT.write_text(json.dumps(report, indent=2) + "\n")
        print(f"\nWritten to {REPORT.name}")
    finally:
        # Unconditional. The rule this obeys was written after two undeclared
        # `SECURITY DEFINER` functions from an earlier investigation were found still
        # installed, readable by PUBLIC, months later.
        async with owner.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        async with owner.connect() as conn:
            left = (
                await conn.execute(
                    text("SELECT count(*) FROM pg_namespace WHERE nspname = :s"), {"s": SCHEMA}
                )
            ).scalar_one()
            definers = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n "
                        "ON n.oid = p.pronamespace WHERE p.prosecdef AND n.nspname = 'public'"
                    )
                )
            ).scalar_one()
            await conn.rollback()
        print(f"scratch schema left behind: {left}; SECURITY DEFINER in public: {definers}")
        await owner.dispose()

    return 0


#: How `python -m eval` finds this sweep. Declared here rather than listed in
#: `__main__.py`, so adding a measurement is adding a file and nothing else.
COMMAND = "quantisation"
USAGE = "quantisation [--subsets N,N,...]"


def run(subsets: tuple[int, ...] = SUBSETS) -> int:
    return asyncio.run(_run(subsets))


def cli(argv: list[str]) -> int:
    """`--subsets N,N,...`, or the default ladder."""
    if "--subsets" in argv:
        return run(tuple(int(n) for n in argv[argv.index("--subsets") + 1].split(",")))
    return run()
