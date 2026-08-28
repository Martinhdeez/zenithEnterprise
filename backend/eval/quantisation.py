# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""What the vector index costs per passage, and what shrinking it costs in recall.

The HNSW index is the structural ceiling on how much corpus one installation can hold.
Measured here rather than assumed: `ix_chunk_embeddings_hnsw` is ~8,940 bytes per vector for
`vector(1024)` at `m=16, ef_construction=64`, and HNSW wants to be resident. At this corpus's
measured passages-per-document that is thousands of gigabytes at a million documents, which
is the number that decides whether the product runs on an enterprise server or does not.

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
#: ceiling is part of the cost.
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


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


async def _build_subset(conn: AsyncConnection, size: int, space: Any) -> dict[str, object]:
    """Copy `size` embeddings into the scratch schema, in all three representations.

    Ordered by `md5(chunk_id)` rather than randomly, so the subsets are reproducible between
    runs and nested within each other: the trend across sizes is then the same vectors plus
    more, rather than four unrelated samples.
    """
    await conn.execute(
        text(
            f"CREATE TABLE {_table(size)} AS "
            "SELECT chunk_id, embedding, "
            "embedding::halfvec(1024) AS embedding_half, "
            "binary_quantize(embedding)::bit(1024) AS embedding_bit "
            "FROM chunk_embeddings "
            "WHERE embedding_model = :model AND embedding_version = :version "
            "ORDER BY md5(chunk_id::text) LIMIT :n"
        ),
        {"model": space.model, "version": space.version, "n": size},
    )
    await conn.execute(text(f"ANALYZE {_table(size)}"))
    rows = (await conn.execute(text(f"SELECT count(*) FROM {_table(size)}"))).scalar_one()

    built: dict[str, object] = {}
    for variant in INDEXES:
        started = time.perf_counter()
        await conn.execute(
            text(
                f"CREATE INDEX {variant.index}_{size} ON {_table(size)} "
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


async def _truth(conn: AsyncConnection, size: int, vectors: list[list[float]]) -> list[list[UUID]]:
    """Exact nearest neighbours, with every index refused.

    `enable_indexscan = off` is not belt and braces: the only honest ground truth for "what
    did the index miss" is a scan that did not use an index. Comparing one approximation
    against another would report the difference between two errors.
    """
    await conn.execute(text("SET LOCAL enable_indexscan = off"))
    await conn.execute(text("SET LOCAL enable_bitmapscan = off"))
    truth: list[list[UUID]] = []
    for vector in vectors:
        rows = await conn.execute(
            text(
                f"SELECT chunk_id FROM {_table(size)} "
                "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
            ),
            {"q": str(vector), "k": max(DEPTHS)},
        )
        truth.append([row.chunk_id for row in rows])
    return truth


async def _probe(
    conn: AsyncConnection,
    size: int,
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
    if variant.rescore is None:
        cast = "vector" if variant.column == "embedding" else "halfvec(1024)"
        started = time.perf_counter()
        rows = await conn.execute(
            text(
                f"SELECT chunk_id FROM {_table(size)} "
                f"ORDER BY {variant.column} <=> CAST(:q AS {cast}) LIMIT :k"
            ),
            {"q": str(vector), "k": limit},
        )
        ids = [row.chunk_id for row in rows]
        return ids, (time.perf_counter() - started) * 1000, 0.0

    started = time.perf_counter()
    rows = await conn.execute(
        text(
            f"SELECT chunk_id FROM {_table(size)} "
            "ORDER BY embedding_bit <~> binary_quantize(CAST(:q AS vector))::bit(1024) "
            "LIMIT :w"
        ),
        {"q": str(vector), "w": variant.rescore},
    )
    shortlist = [row.chunk_id for row in rows]
    index_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    rows = await conn.execute(
        text(
            f"SELECT chunk_id FROM {_table(size)} WHERE chunk_id = ANY(:ids) "
            "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
        ),
        {"ids": [str(chunk_id) for chunk_id in shortlist], "q": str(vector), "k": limit},
    )
    ids = [row.chunk_id for row in rows]
    return ids, index_ms, (time.perf_counter() - started) * 1000


async def _index_recall(
    conn: AsyncConnection,
    size: int,
    vectors: list[list[float]],
    truth: list[list[UUID]],
    ef_search: int,
) -> dict[str, dict[str, object]]:
    """Every variant against the exact neighbours, at every depth."""
    await conn.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))
    measured: dict[str, dict[str, object]] = {}

    for variant in VARIANTS:
        overlaps: dict[int, list[float]] = {depth: [] for depth in DEPTHS}
        index_times: list[float] = []
        rescore_times: list[float] = []

        for vector, exact_ids in zip(vectors, truth, strict=True):
            got: list[UUID] = []
            fastest_index: float | None = None
            fastest_rescore: float | None = None
            for _ in range(REPEATS):
                got, index_ms, rescore_ms = await _probe(conn, size, variant, vector, max(DEPTHS))
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
            f"{row['index_median_ms']:.2f} + {row['rescore_median_ms']:.2f} ms",
            flush=True,
        )
    return measured


async def _end_to_end(
    where: Installation,
    size: int,
    variant: Variant,
    ef_search: int,
) -> dict[str, object]:
    """The real pipeline with only the dense half swapped for the quantised one.

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
                dense_ids, _, _ = await _probe(scratch, size, variant, embedding, CANDIDATES)
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
            production_index_bytes = int(
                (
                    await conn.execute(text("SELECT pg_relation_size('ix_chunk_embeddings_hnsw')"))
                ).scalar_one()
            )

            for size in sizes:
                print(f"  {size} vectors:")
                built = await _build_subset(conn, size, where.space)
                truth = await _truth(conn, size, vectors)
                recall = await _index_recall(conn, size, vectors, truth, ef_search)
                by_size[size] = {**built, "index": recall}

        full = sizes[-1]
        print("\n  end to end, scored as live.py scores, at the full corpus:")
        reached: dict[str, object] = {}
        for variant in VARIANTS:
            measured = await _end_to_end(where, full, variant, ef_search)
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
                "production_bloat_note": (
                    "The live index is larger per vector than a freshly built one of the "
                    "same parameters. The difference is ingestion churn, not representation, "
                    "and every ratio below is fresh-against-fresh."
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
                "pgvector raises the effective ef_search to the query's LIMIT, so a rescore "
                "width above hnsw_ef_search widens the graph walk as well as the shortlist. "
                "Part of the cost of a wide rescore is that, not the rescore alone.",
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
