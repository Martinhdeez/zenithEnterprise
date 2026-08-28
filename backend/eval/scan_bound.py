# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""What bounds an iterative scan, on a corpus where the bound can actually bind.

`perf/iterative-scan-on-the-hot-path` turned `hnsw.iterative_scan` on for every query and
was right to: HNSW takes its `ef_search` best candidates from the graph every tenant shares,
and RLS discards the other tenants' rows *afterwards*, so an unscoped query is not an
unfiltered one. Measured there, ten of forty-two questions returned **zero** dense candidates
under an ordinary label filter, and iterative scan took every one of them back to 50 of 50.

It left one constant unset, and honestly. pgvector 0.8 stops an iterative scan at
`hnsw.max_scan_tuples`, whose default is **20,000** — more than the entire 13,549-vector
graph that installation has. No value could bind, nothing could be calibrated, and ADR 0005
is explicit that an unmeasured constant does not get written down.

## The regime the constant is for, and why it does not need a huge corpus

The instinct is that this needs millions of vectors. It does not. `max_scan_tuples` binds
when the scan has to walk far to find rows that survive the filter, and how far that is
depends on **selectivity**, not on size:

    tuples walked  ~  wanted / fraction of the graph the filter admits

Fifty candidates at one tenant in a thousand is fifty thousand tuples, and it is fifty
thousand whether the graph holds 150,000 rows or 150 million. So a corpus of 150,000 with a
0.05% filter reaches the same regime as a corpus of 150 million with the same filter, at a
fraction of the build. **Selectivity is the axis; size is not.**

That is the whole design, and it is also the reason this is worth measuring at all: the
multi-tenant future *is* the low-selectivity regime. A tenant holding 1% of an installation's
passages is a 1% filter on every query it makes.

## What is being traded

Below the bound, the scan stops early and returns fewer candidates than asked for — the
silent under-return iterative scan was turned on to prevent, reintroduced by a constant.
Above it, a query whose filter admits almost nothing walks the graph until the global 10 s
`statement_timeout` stops it, which turns one unlucky query into a 500 and, under load, into
a wave of them.

So the number wanted is the smallest bound that still fills the candidate set at the
selectivities this product will actually see. Both failures are measured here rather than
argued: `returned` says whether the set filled, `ms` says what it cost.

**Read-only against the product's data.** Vectors are copied into `zenith_scan_bound`, which
is dropped in a `finally`; `chunk_embeddings` is read and never written.

    docker compose exec -T api python -m eval scan-bound
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.core.config import settings
from app.core.hardware import active as active_profile
from app.features.embeddings.client import MODEL, VERSION, TeiClient
from app.features.retrieval.search import CANDIDATES
from eval.questions import load_questions

REPORT = Path(__file__).parent / "scan-bound.json"
SCHEMA = "zenith_scan_bound"

#: Enough graph that the planner keeps choosing HNSW rather than falling back to an exact
#: scan, which is what happens on a small table and is what hid this class of bug before.
#: More than this buys nothing here: selectivity is the axis, not size.
VECTORS = 150_000

#: The fraction of the graph the filter admits, i.e. one tenant's share of an installation.
#: 0.5 is the two-tenant case this corpus already is; 0.0005 is a tenant holding one passage
#: in two thousand, which is an ordinary enterprise installation with a few hundred customers.
SELECTIVITY = (0.5, 0.1, 0.02, 0.005, 0.001)

#: `None` means leave pgvector's default in place, and it is first so the report opens with
#: the behaviour that ships today.
BOUNDS: tuple[int | None, ...] = (None, 20_000, 100_000, 500_000, 2_000_000)

#: What the product asks the dense half for. Imported rather than repeated: a bound that
#: fills 50 is not evidence about a pipeline that wants 50 of something else.
WANTED = CANDIDATES


async def _build(conn: AsyncConnection) -> tuple[int, int]:
    """Interpolated vectors with a synthetic tenant column, and one shared graph.

    The vectors come from `eval/scale.py`'s generator — interpolation between near
    neighbours — because a filter experiment still needs a realistically clustered space:
    on random vectors every neighbourhood looks the same and walking the graph would find
    admissible rows at a rate that has nothing to do with this product.

    The tenant column is assigned by hash rather than by cluster, deliberately. Tenants whose
    documents sit together in embedding space would make the filter *easier* than the real
    thing, because the walk would find a run of admissible rows at once. Scattering them is
    the pessimistic assignment and the honest one.
    """
    # A parallel plan on the interpolation step asks for a shared memory segment far larger
    # than the 64 MB Docker gives a container by default, and fails with `DiskFull` — which
    # names the wrong resource and sends you to check the disk, where there is 389 GB free.
    # Serial here rather than raising `shm_size` in the compose file: this is laboratory
    # tooling and it should not require a change to how the product is deployed to run.
    await conn.execute(text("SET LOCAL max_parallel_workers_per_gather = 0"))
    await conn.execute(text("SET LOCAL max_parallel_maintenance_workers = 0"))
    await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
    await conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
    await conn.execute(
        text(
            f"CREATE TABLE {SCHEMA}.seed AS SELECT "
            "row_number() OVER (ORDER BY md5(chunk_id::text))::int AS id, "
            "l2_normalize(embedding) AS embedding FROM chunk_embeddings "
            "WHERE embedding_model = :model AND embedding_version = :version"
        ),
        {"model": MODEL, "version": VERSION},
    )
    await conn.execute(text(f"ALTER TABLE {SCHEMA}.seed ALTER COLUMN embedding TYPE vector(1024)"))
    await conn.execute(text(f"ALTER TABLE {SCHEMA}.seed ADD PRIMARY KEY (id)"))
    await conn.execute(text("SET LOCAL maintenance_work_mem = '400MB'"))
    await conn.execute(
        text(
            f"CREATE INDEX ON {SCHEMA}.seed USING hnsw (embedding vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 64)"
        )
    )
    await conn.execute(text("SET LOCAL hnsw.ef_search = 60"))
    await conn.execute(
        text(
            f"CREATE TABLE {SCHEMA}.pairs AS SELECT s.id AS a, n.id AS b "
            f"FROM {SCHEMA}.seed s CROSS JOIN LATERAL ("
            f"  SELECT t.id FROM {SCHEMA}.seed t WHERE t.id <> s.id"
            "  ORDER BY t.embedding <=> s.embedding LIMIT 20) n"
        )
    )
    await conn.execute(
        text(
            f"CREATE TABLE {SCHEMA}.vecs AS SELECT * FROM ("
            "SELECT row_number() OVER (ORDER BY md5(p.a::text||':'||p.b::text||':'||w.t::text)) "
            "         AS id, "
            "  l2_normalize(CASE w.t "
            "    WHEN 1 THEN va.embedding + vb.embedding "
            "    WHEN 2 THEN va.embedding + va.embedding + vb.embedding "
            "    WHEN 3 THEN va.embedding + vb.embedding + vb.embedding "
            "    WHEN 4 THEN va.embedding + va.embedding + va.embedding + vb.embedding "
            "    ELSE        va.embedding + vb.embedding + vb.embedding + vb.embedding "
            "  END)::halfvec(1024) AS embedding "
            f"  FROM {SCHEMA}.pairs p "
            f"  JOIN {SCHEMA}.seed va ON va.id = p.a "
            f"  JOIN {SCHEMA}.seed vb ON vb.id = p.b "
            "  CROSS JOIN (VALUES (1),(2),(3),(4),(5)) w(t)) g "
            f"WHERE g.id <= {VECTORS}"
        )
    )
    # A uniform draw in [0,1) per row, so any selectivity is `bucket < fraction` and every
    # arm filters the same graph rather than a differently-shaped one.
    await conn.execute(text(f"ALTER TABLE {SCHEMA}.vecs ADD COLUMN bucket double precision"))
    await conn.execute(
        text(
            f"UPDATE {SCHEMA}.vecs SET bucket = "
            "('x' || substr(md5(id::text), 1, 8))::bit(32)::int8 / 4294967296.0"
        )
    )
    await conn.execute(text(f"ALTER TABLE {SCHEMA}.vecs ADD PRIMARY KEY (id)"))
    await conn.execute(text(f"CREATE INDEX ON {SCHEMA}.vecs (bucket)"))
    await conn.execute(text("SET LOCAL maintenance_work_mem = '400MB'"))
    await conn.execute(
        text(
            # `halfvec`, because migration 0025 made that the index the product has. An
            # fp32 index here would be measuring a shape nothing runs, and would take three
            # times the space to do it.
            f"CREATE INDEX ON {SCHEMA}.vecs USING hnsw (embedding halfvec_cosine_ops) "
            "WITH (m = 16, ef_construction = 64)"
        )
    )
    await conn.execute(text(f"ANALYZE {SCHEMA}.vecs"))
    rows = int((await conn.execute(text(f"SELECT count(*) FROM {SCHEMA}.vecs"))).scalar_one())
    size = int(
        (
            await conn.execute(text(f"SELECT pg_relation_size('{SCHEMA}.vecs_embedding_idx')"))
        ).scalar_one()
    )
    return rows, size


async def _plan(
    conn: AsyncConnection,
    vector: list[float],
    fraction: float,
    bound: int | None,
    ef_search: int,
) -> str:
    """Which strategy the planner actually chose.

    Without this the report cannot tell its two possible null results apart, and they mean
    opposite things. Either the bound never binds because iterative scan comfortably fills
    the candidate set — in which case the default is right — or the planner stopped using
    the vector index at all and the arm measured something else entirely, in which case the
    experiment never reached the regime it was built for and proves nothing.

    Latency falling as the filter tightens is the tell for the second, and it is what the
    first run of this file showed. So the plan is now recorded rather than inferred.
    """
    # The same session settings the timed query runs under. Explaining a differently
    # configured query and labelling the timing with it was the first version of this
    # function, and it reported `Gather Merge` for an arm whose measured query used the
    # index — a plan for a query nobody ran.
    await conn.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))
    await conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    if bound is not None:
        await conn.execute(text(f"SET LOCAL hnsw.max_scan_tuples = {int(bound)}"))

    rows = await conn.execute(
        text(
            f"EXPLAIN (COSTS OFF) SELECT id FROM {SCHEMA}.vecs WHERE bucket < :f "
            "ORDER BY embedding <=> CAST(:q AS halfvec(1024)) LIMIT :k"
        ),
        {"f": fraction, "q": str(vector), "k": WANTED},
    )
    # `Parallel` prefixes every node type, so matching on the node name has to allow it.
    # Missing that was how a parallel sequential scan came back as an unclassified fallback
    # string rather than as the seq scan it was.
    lines = [str(row[0]).strip().removeprefix("->  ").removeprefix("Parallel ") for row in rows]
    for line in lines:
        if line.startswith("Index Scan using") and "embedding" in line:
            return "hnsw"
        if line.startswith("Seq Scan"):
            return "seq"
        if line.startswith(("Bitmap Heap Scan", "Bitmap Index Scan")):
            return "bitmap"
        if line.startswith("Index Scan using") or line.startswith("Index Only Scan using"):
            return "btree_on_filter"
    return "unclassified:" + "|".join(lines[:3])


async def _arm(
    conn: AsyncConnection,
    queries: list[list[float]],
    fraction: float,
    bound: int | None,
    ef_search: int,
) -> dict[str, object]:
    """One selectivity at one bound, over every question."""
    returned: list[int] = []
    times: list[float] = []
    plan = await _plan(conn, queries[0], fraction, bound, ef_search)

    for vector in queries:
        await conn.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))
        await conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        if bound is not None:
            await conn.execute(text(f"SET LOCAL hnsw.max_scan_tuples = {int(bound)}"))
        started = time.perf_counter()
        rows = await conn.execute(
            text(
                f"SELECT id FROM {SCHEMA}.vecs WHERE bucket < :f "
                "ORDER BY embedding <=> CAST(:q AS halfvec(1024)) LIMIT :k"
            ),
            {"f": fraction, "q": str(vector), "k": WANTED},
        )
        got = len(rows.all())
        times.append((time.perf_counter() - started) * 1000)
        returned.append(got)

    ordered = sorted(times)
    return {
        "plan": plan,
        "returned_mean": round(statistics.fmean(returned), 2),
        "returned_min": min(returned),
        "filled": sum(1 for r in returned if r >= WANTED),
        "short": sum(1 for r in returned if r < WANTED),
        "median_ms": round(statistics.median(times), 2),
        "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 2),
        "max_ms": round(max(times), 2),
    }


async def _run() -> int:
    engine = create_async_engine(settings.database_owner_url)
    profile = active_profile()
    started = time.perf_counter()

    questions = [q.question for q in load_questions()]
    queries = await TeiClient(profile=profile).embed(questions)
    print(f"{len(queries)} questions embedded")

    results: list[dict[str, object]] = []
    try:
        async with engine.begin() as conn:
            vectors, index_bytes = await _build(conn)
            print(f"built {vectors:,} vectors, index {index_bytes / 1e6:.0f} MB\n")

        async with engine.connect() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            for fraction in SELECTIVITY:
                admitted = int(VECTORS * fraction)
                print(f"  selectivity {fraction:<7} ({admitted:>7,} rows admitted)")
                for bound in BOUNDS:
                    measured = await _arm(conn, queries, fraction, bound, profile.hnsw_ef_search)
                    label = "default(20000)" if bound is None else f"{bound:,}"
                    results.append(
                        {"selectivity": fraction, "admitted": admitted, "bound": bound, **measured}
                    )
                    print(
                        f"    {label:<16} {str(measured['plan']):<16} "
                        f"returned {measured['returned_mean']:>5} "
                        f"(min {measured['returned_min']:>2}, short {measured['short']:>2})"
                        f"   median {measured['median_ms']:>8} ms",
                        flush=True,
                    )
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        async with engine.connect() as conn:
            left = int(
                (
                    await conn.execute(
                        text("SELECT count(*) FROM pg_namespace WHERE nspname = :s"), {"s": SCHEMA}
                    )
                ).scalar_one()
            )
        await engine.dispose()

    REPORT.write_text(
        json.dumps(
            {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "took_s": round(time.perf_counter() - started, 1),
                "profile": profile.name,
                "hnsw_ef_search": profile.hnsw_ef_search,
                "wanted": WANTED,
                "questions": len(queries),
                "corpus": {"vectors": vectors, "index_bytes": index_bytes},
                "arms": results,
                "scratch_schemas_left": left,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"\nscratch schemas left: {left}")
    print(f"Written to {REPORT.name}")
    return 0


COMMAND = "scan-bound"
USAGE = "scan-bound"


def run() -> int:
    return asyncio.run(_run())
