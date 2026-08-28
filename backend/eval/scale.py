# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""Does binary quantisation fail at the corpus size this product is aimed at?

`quantisation.json` measured the three precisions over the real 13,549 vectors and found the
gap to exact retrieval **widening with corpus size**: `+0.0384` per decade at rescore 100.
Extrapolated log-linearly to 300M passages that is recall@10 ≈ 0.79, which is not shippable.
Binary is the difference between roughly 156,000 and 980,000 documents on one server, so the
extrapolation is worth more than an extrapolation.

The corpus that would settle it does not exist and cannot be downloaded. This builds one.

## Why quantisation fails, and therefore what has to be reproduced

Binary quantisation keeps one bit per dimension — the sign. Two vectors whose 1024 signs
largely agree are indistinguishable in Hamming space **however far apart they truly are**.
How often that happens is a property of the *local density* of the embedding space, not of
the row count. Row count is only a proxy: a corpus grows denser as it grows, and it is the
density that breaks the index.

So the quantity to reproduce is density, and the quantity to report against is density.

## The generator, and the two ways it could have lied

Points are made by interpolating **between near neighbours**: take a real vector, take one of
its twenty nearest, take a point between them on the sphere. That densifies regions the real
corpus already occupies, which is what happens when a real archive grows — more documents
arrive on subjects the archive already covers.

The two obvious alternatives are both wrong, in opposite directions, and neither is obviously
wrong until it is measured:

- **Random unit vectors** in 1024 dimensions are nearly orthogonal. No clusters, Hamming
  distances separate cleanly, and binary would look far better than it is.
- **Real vectors plus Gaussian noise** builds a tight shell around every original. Density
  explodes, collisions with it, and binary would look far worse than it is.

Interpolation avoids both, and it still does not produce a corpus that *is* a larger real
one. Measured here: at 13,549 rows the synthetic set's mean distance to the tenth nearest
neighbour is **0.1773** against the real corpus's **0.2589**. It is denser than a real corpus
of the same count, and its density falls faster as it grows (exponent ≈ −0.30 to −0.56
against the real corpus's −0.135).

**That is not corrected, it is used.** Being denser than its row count suggests is precisely
what makes a corpus of 150,000 rows able to stand in for the regime under investigation: at
that size its local density already matches a *real* corpus of some hundreds of millions of
passages. The row count is discarded as meaningless and the measured density is carried
instead.

## The translation, and the gate that licenses it

The real corpus's own density law is measured here too, by subsetting it: least squares over
its own subsets gives `d10 ∝ N^−0.107`, an intrinsic dimensionality of about 9.4. That law
converts any measured density into the real row count that would produce it, which is how a
synthetic subset gets an `equivalent_real_n` — a translation that is an ordering rather than a
quantity once it is extrapolated far past the decade it was fitted over.

The whole construction rests on one claim — that **matched density implies matched
quantisation error** — and that claim is testable without believing it first. There is a
synthetic subset whose density equals the real corpus's, and it is much smaller than 13,549.
Binary's gap on that subset must equal binary's gap on the real 13,549.

If it does, the model is calibrated and everything above it is readable. **If it does not,
the report says so and the synthetic numbers are discarded** — which is a result too, and a
cheaper one than shipping an index built on a hope.

## What is measured, and what is deliberately not

Quantisation error **alone**, by exact scan, with every index refused. `quantisation.json`
measured quantisation and HNSW approximation together, which is right for choosing what to
ship and wrong for asking which of the two grows: an index-based number that degrades could
be the compression or could be the graph. Here the graph is removed.

Latency is not reported. This runs in a 7.75 GB VM against tables that do not fit in
`shared_buffers`, so every figure would describe memory pressure on a laptop rather than the
target machine. Recall does not care where the pages were.

**Read-only against the product's data.** Everything is built inside `zenith_scale`, which is
dropped in a `finally`; `chunk_embeddings` is read and never written.

    docker compose exec -T api python -m eval scale [--sizes N,N,...]
"""

from __future__ import annotations

import asyncio
import json
import math
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.core.config import settings
from app.core.hardware import active as active_profile
from app.features.embeddings.client import MODEL, VERSION, TeiClient
from eval.questions import load_questions

REPORT = Path(__file__).parent / "scale.json"
SCHEMA = "zenith_scale"

#: Interpolants per neighbour pair, as integer combinations: `l2_normalize(a + b)` is the
#: midpoint, `a + a + b` is one third of the way, and so on. pgvector has no scalar multiply,
#: and going through `real[]` to get one would cost an array round trip per row for a
#: continuous `t` that buys nothing — five positions between two points already fill the
#: segment.
INTERPOLANTS = 5

#: Nearest neighbours per seed vector. Twenty is enough to keep interpolants inside the
#: cluster a document belongs to; far more would start bridging unrelated subjects and invent
#: manifold that the real corpus does not have.
NEIGHBOURS = 20

#: The ladder. `3750` is not a round number and is not meant to be: it is where the synthetic
#: set's fitted density law crosses the real corpus's measured density, so it is the point the
#: calibration gate compares. The rest extend upward.
SIZES = (3_750, 13_549, 50_000, 150_000)

#: How many exact neighbours count as the answer. Ten, as in `quantisation.json`, so the two
#: reports are readable side by side.
DEPTH = 10

#: Hamming candidates re-ordered by exact cosine before the ten are chosen.
RESCORE = (20, 50, 100, 200, 400)

#: Probes for the density statistic. It is a mean over a sample, not a census, and the sample
#: only has to be large enough to place the corpus on a curve.
DENSITY_PROBES = 60

#: What the fit came back with on 2026-08-28, recorded so a future run that disagrees is
#: visible rather than silent. Nothing reads it: `_real_density_law` refits every time, because
#: a corpus that grows changes this and a hard-coded exponent would quietly mistranslate every
#: density in the report.
FITTED_EXPONENT_2026_08_28 = -0.1068


@dataclass(frozen=True, slots=True)
class Gaps:
    """One corpus, one set of questions. Gap is `1 - recall@10` against exact fp32."""

    questions: int
    fp16: float
    binary: dict[int, float]
    binary_worst: dict[int, float]


@dataclass(frozen=True, slots=True)
class Measured:
    size: int
    density: float
    equivalent_real_n: float
    answerable: Gaps
    unanswerable: Gaps
    combined: Gaps


def _gaps(fp16: list[int], binary: dict[int, list[int]], keep: list[int]) -> Gaps:
    """Reduce per-query hits to gaps, over the subset of questions `keep` selects.

    Split because the two kinds measure different things. An unanswerable question has no
    meaningful exact top ten — the nearest passages to "how many goals did Messi score" over
    employment law are an arbitrary set — so asking a compressed index to reproduce it is
    measuring the faithful reproduction of noise. Included anyway, and reported separately,
    because a real installation is asked those questions and F26 exists for them.
    """
    total = len(keep) * DEPTH
    return Gaps(
        questions=len(keep),
        fp16=round(1 - sum(fp16[i] for i in keep) / total, 4),
        binary={w: round(1 - sum(binary[w][i] for i in keep) / total, 4) for w in RESCORE},
        binary_worst={w: round(min(binary[w][i] for i in keep) / DEPTH, 4) for w in RESCORE},
    )


def _fit(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Least squares on log-log. Returns (exponent, intercept at N = 1)."""
    xs = [math.log10(n) for n, _ in points]
    ys = [math.log10(d) for _, d in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / denom
    return slope, my - slope * mx


async def _density(conn: AsyncConnection, table: str, limit: int, probe_from: str) -> float:
    """Mean cosine distance to the tenth nearest neighbour.

    The mechanism binary quantisation fails through, reduced to one number. Exact, with
    indexes refused: an approximate neighbour would make the density statistic itself an
    artefact of the thing being measured.
    """
    await conn.execute(text("SET LOCAL enable_indexscan = off"))
    await conn.execute(text("SET LOCAL enable_bitmapscan = off"))
    rows = await conn.execute(
        text(
            f"SELECT avg(d) FROM (SELECT id FROM {probe_from} ORDER BY md5(id::text) "
            f"LIMIT {DENSITY_PROBES}) p CROSS JOIN LATERAL ("
            f"  SELECT (s.embedding <=> q.embedding) AS d"
            f"  FROM {table} s, {table} q"
            f"  WHERE q.id = p.id AND s.id <> p.id AND s.id <= {limit}"
            f"  ORDER BY s.embedding <=> q.embedding OFFSET {DEPTH - 1} LIMIT 1) x"
        )
    )
    return float(rows.scalar_one())


async def _build(conn: AsyncConnection) -> dict[str, object]:
    """Seed, neighbours, interpolants. Idempotent — it drops what it made before."""
    await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
    await conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))

    # Normalised on the way in. Cosine distance and `binary_quantize` are both scale
    # invariant so it changes no measurement, but interpolation between unnormalised vectors
    # would weight the longer one and put the midpoint somewhere other than the middle.
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
    # The seed table is measured too — it is the real corpus, and the gate compares against
    # it — so it carries the same three representations the synthetic one does.
    await conn.execute(
        text(
            f"ALTER TABLE {SCHEMA}.seed "
            "ADD COLUMN embedding_half halfvec(1024), ADD COLUMN embedding_bit bit(1024)"
        )
    )
    await conn.execute(
        text(
            f"UPDATE {SCHEMA}.seed SET embedding_half = embedding::halfvec(1024), "
            "embedding_bit = binary_quantize(embedding)::bit(1024)"
        )
    )
    await conn.execute(text(f"ALTER TABLE {SCHEMA}.seed ADD PRIMARY KEY (id)"))
    seeds = int((await conn.execute(text(f"SELECT count(*) FROM {SCHEMA}.seed"))).scalar_one())

    await conn.execute(text("SET LOCAL maintenance_work_mem = '400MB'"))
    await conn.execute(
        text(
            f"CREATE INDEX ON {SCHEMA}.seed USING hnsw (embedding vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 64)"
        )
    )
    # Approximate neighbours are fine here and exact ones would cost an O(n²) scan: this
    # chooses *which* points to interpolate between, and a slightly different twentieth
    # neighbour is a slightly different synthetic point, not a wrong one.
    await conn.execute(text("SET LOCAL hnsw.ef_search = 60"))
    await conn.execute(
        text(
            f"CREATE TABLE {SCHEMA}.pairs AS SELECT s.id AS a, n.id AS b "
            f"FROM {SCHEMA}.seed s CROSS JOIN LATERAL ("
            f"  SELECT t.id FROM {SCHEMA}.seed t WHERE t.id <> s.id"
            f"  ORDER BY t.embedding <=> s.embedding LIMIT {NEIGHBOURS}) n"
        )
    )

    # `md5` of the triple, so the ordering is deterministic between runs and the subsets are
    # nested: a larger size is the smaller one plus more, not a different sample.
    await conn.execute(
        text(
            f"CREATE TABLE {SCHEMA}.vecs AS SELECT "
            "row_number() OVER (ORDER BY md5(p.a::text||':'||p.b::text||':'||w.t::text)) AS id, "
            "l2_normalize(CASE w.t "
            "  WHEN 1 THEN va.embedding + vb.embedding "
            "  WHEN 2 THEN va.embedding + va.embedding + vb.embedding "
            "  WHEN 3 THEN va.embedding + vb.embedding + vb.embedding "
            "  WHEN 4 THEN va.embedding + va.embedding + va.embedding + vb.embedding "
            "  ELSE        va.embedding + vb.embedding + vb.embedding + vb.embedding "
            "END)::vector(1024) AS embedding, "
            "NULL::halfvec(1024) AS embedding_half, NULL::bit(1024) AS embedding_bit "
            f"FROM {SCHEMA}.pairs p "
            f"JOIN {SCHEMA}.seed va ON va.id = p.a "
            f"JOIN {SCHEMA}.seed vb ON vb.id = p.b "
            f"CROSS JOIN (VALUES (1),(2),(3),(4),(5)) w(t)"
        )
    )
    await conn.execute(
        text(
            f"UPDATE {SCHEMA}.vecs SET embedding_half = embedding::halfvec(1024), "
            "embedding_bit = binary_quantize(embedding)::bit(1024)"
        )
    )
    await conn.execute(text(f"ALTER TABLE {SCHEMA}.vecs ADD PRIMARY KEY (id)"))
    await conn.execute(text(f"ANALYZE {SCHEMA}.vecs"))
    generated = int((await conn.execute(text(f"SELECT count(*) FROM {SCHEMA}.vecs"))).scalar_one())
    return {"seed_vectors": seeds, "pairs": seeds * NEIGHBOURS, "generated": generated}


async def _real_density_law(conn: AsyncConnection) -> dict[str, object]:
    """How fast the *real* corpus densifies. The only thing that converts density into N."""
    points: list[tuple[float, float]] = []
    for size in (3_000, 6_000, 13_549):
        points.append((size, await _density(conn, f"{SCHEMA}.seed", size, f"{SCHEMA}.seed")))
    exponent, intercept = _fit(points)
    return {
        "points": [{"n": n, "d10": round(d, 4)} for n, d in points],
        "exponent": round(exponent, 4),
        "intercept_log10": round(intercept, 4),
        "intrinsic_dimension": round(-1 / exponent, 1) if exponent else None,
    }


#: The range the real corpus's density law was actually fitted over. Beyond it the law is
#: being extrapolated, and `equivalent_real_n` stops being a number and becomes an ordering.
FITTED_OVER = (3_000, 13_549)


def _equivalent(density: float, exponent: float, intercept: float) -> float:
    """The real row count whose local density is this one. The whole translation.

    **Read the largest of these as a direction, not a quantity.** The law is fitted over
    3,000 to 13,549 real vectors, a little under one decade. Asking it for the row count
    behind a density three decades below anything it saw returns a number with a great many
    digits and roughly one of them meaningful. What survives the extrapolation is the
    ordering — this corpus is denser than that one, and denser than any real corpus of a size
    worth planning for — and the ordering is all the conclusion needs.
    """
    return 10 ** ((math.log10(density) - intercept) / exponent)


async def _truth_and_gaps(
    conn: AsyncConnection, table: str, limit: int, queries: list[list[float]]
) -> tuple[list[int], dict[int, list[int]]]:
    """Exact fp32 neighbours, then the same question asked of each compressed form.

    Every index is refused throughout. What is being measured is what the *compression* loses,
    not what the graph loses on top of it — `quantisation.json` measured the two together,
    which is the right number for choosing what to ship and the wrong one for asking which of
    the two grows with the corpus.
    """
    await conn.execute(text("SET LOCAL enable_indexscan = off"))
    await conn.execute(text("SET LOCAL enable_bitmapscan = off"))

    # Per query rather than summed, so the caller can split answerable from unanswerable
    # without paying for a second pass over the table.
    fp16_hits: list[int] = []
    binary_hits: dict[int, list[int]] = {width: [] for width in RESCORE}

    for vector in queries:
        params: dict[str, Any] = {"q": str(vector), "k": DEPTH, "n": limit}
        truth = {
            row.id
            for row in await conn.execute(
                text(
                    f"SELECT id FROM {table} WHERE id <= :n "
                    "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
                ),
                params,
            )
        }

        fp16 = {
            row.id
            for row in await conn.execute(
                text(
                    f"SELECT id FROM {table} WHERE id <= :n "
                    "ORDER BY embedding_half <=> CAST(:q AS halfvec(1024)) LIMIT :k"
                ),
                params,
            )
        }
        fp16_hits.append(len(truth & fp16))

        for width in RESCORE:
            rows = await conn.execute(
                text(
                    f"SELECT id FROM (SELECT id, embedding FROM {table} WHERE id <= :n "
                    "  ORDER BY embedding_bit <~> binary_quantize(CAST(:q AS vector))"
                    "  LIMIT :w) c "
                    "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
                ),
                {**params, "w": width},
            )
            binary_hits[width].append(len(truth & {row.id for row in rows}))

    return fp16_hits, binary_hits


async def _hygiene(conn: AsyncConnection) -> dict[str, int]:
    schemas = int(
        (
            await conn.execute(
                text("SELECT count(*) FROM pg_namespace WHERE nspname = :s"), {"s": SCHEMA}
            )
        ).scalar_one()
    )
    definers = int(
        (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE p.prosecdef AND n.nspname = 'public'"
                )
            )
        ).scalar_one()
    )
    return {"scratch_schemas_left": schemas, "security_definer_in_public": definers}


async def _run(sizes: tuple[int, ...]) -> int:
    engine = create_async_engine(settings.database_owner_url)
    started = time.perf_counter()

    loaded = load_questions()
    embedder = TeiClient(profile=active_profile())
    queries = await embedder.embed([q.question for q in loaded])
    answerable = [i for i, q in enumerate(loaded) if q.sources]
    unanswerable = [i for i, q in enumerate(loaded) if not q.sources]
    everything = list(range(len(loaded)))
    print(f"{len(answerable)} answerable + {len(unanswerable)} unanswerable questions\n")

    try:
        async with engine.begin() as conn:
            built = await _build(conn)
            print(f"built: {built}\n")

        async with engine.connect() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            law = await _real_density_law(conn)
            exponent = float(law["exponent"])  # type: ignore[arg-type]
            intercept = float(law["intercept_log10"])  # type: ignore[arg-type]
            print(f"real corpus density law: d10 ~ N^{exponent}  (d~{law['intrinsic_dimension']})")

            real_density = await _density(conn, f"{SCHEMA}.seed", 13_549, f"{SCHEMA}.seed")
            print(f"real corpus d10 at 13,549: {real_density:.4f}\n")

            # The real corpus, measured by this file's own rule, so the gate compares like
            # with like rather than against a number another harness computed differently.
            fp16_hits, binary_hits = await _truth_and_gaps(conn, f"{SCHEMA}.seed", 13_549, queries)
            real = _gaps(fp16_hits, binary_hits, answerable)
            real_all = _gaps(fp16_hits, binary_hits, everything)
            print(
                f"REAL 13,549   d10 {real_density:.4f}   binary r100 "
                f"{real.binary[100]:.4f} answerable / {real_all.binary[100]:.4f} all\n"
            )

            measured: list[Measured] = []
            for size in sizes:
                density = await _density(conn, f"{SCHEMA}.vecs", size, f"{SCHEMA}.vecs")
                fp16_hits, binary_hits = await _truth_and_gaps(
                    conn, f"{SCHEMA}.vecs", size, queries
                )
                equivalent = _equivalent(density, exponent, intercept)
                measured.append(
                    Measured(
                        size,
                        density,
                        equivalent,
                        _gaps(fp16_hits, binary_hits, answerable),
                        _gaps(fp16_hits, binary_hits, unanswerable),
                        _gaps(fp16_hits, binary_hits, everything),
                    )
                )
                latest = measured[-1].answerable
                print(
                    f"  {size:>9,}  d10 {density:.4f}  = real {equivalent:>15,.0f}"
                    f"   fp16 {latest.fp16:.4f}   binary r100 {latest.binary[100]:.4f}"
                    f"   r400 {latest.binary[400]:.4f}",
                    flush=True,
                )
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        async with engine.connect() as conn:
            hygiene = await _hygiene(conn)
        await engine.dispose()

    # The gate. The synthetic subset whose density is closest to the real corpus's must
    # reproduce the real corpus's binary gap; if it does not, matched density does not imply
    # matched error and none of the rows above mean anything.
    nearest = min(measured, key=lambda m: abs(m.density - real_density))
    deltas = {w: round(nearest.answerable.binary[w] - real.binary[w], 4) for w in RESCORE}
    worst_delta = max(abs(d) for d in deltas.values())
    calibrated = worst_delta <= 0.05
    # Magnitude was the whole test in the first version of this file, and the sign turned out
    # to carry more than the magnitude did: every delta came back negative, meaning the
    # synthetic corpus is *kinder* to binary quantisation than the real one at a density that
    # is if anything higher. Density is therefore not the whole mechanism.
    #
    # It does not invalidate the run, and the direction is why. A synthetic corpus that
    # flatters binary and still shows it failing bounds the real degradation from below: the
    # real thing is at least this bad. A bias towards the conclusion would have been fatal;
    # a bias away from it is a floor.
    bias = (
        "flatters_binary"
        if all(d < 0 for d in deltas.values())
        else ("penalises_binary" if all(d > 0 for d in deltas.values()) else "mixed")
    )

    REPORT.write_text(
        json.dumps(
            {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "took_s": round(time.perf_counter() - started, 1),
                "questions": {
                    "answerable": len(answerable),
                    "unanswerable": len(unanswerable),
                },
                "depth": DEPTH,
                "corpus": built,
                "real_density_law": law,
                "real": {
                    "n": 13_549,
                    "d10": round(real_density, 4),
                    "answerable": asdict(real),
                    "all_questions": asdict(real_all),
                },
                "gate": {
                    "compared_size": nearest.size,
                    "compared_density": round(nearest.density, 4),
                    "real_density": round(real_density, 4),
                    "delta_by_rescore": deltas,
                    "worst_delta": round(worst_delta, 4),
                    "tolerance": 0.05,
                    "calibrated": calibrated,
                    "systematic_bias": bias,
                    "bias_means": {
                        "flatters_binary": "synthetic understates the gap; real degradation "
                        "is at least what is reported here",
                        "penalises_binary": "synthetic overstates the gap; the report is an "
                        "upper bound and binary may survive",
                        "mixed": "no systematic direction",
                    }[bias],
                },
                "synthetic": [
                    {
                        "size": m.size,
                        "d10": round(m.density, 4),
                        "equivalent_real_n": round(m.equivalent_real_n),
                        "answerable": asdict(m.answerable),
                        "unanswerable": asdict(m.unanswerable),
                        "all_questions": asdict(m.combined),
                    }
                    for m in measured
                ],
                "hygiene": hygiene,
            },
            indent=2,
        )
        + "\n"
    )

    print(f"\ngate: {'CALIBRATED' if calibrated else 'FAILED'}  worst delta {worst_delta:.4f}")
    if not calibrated:
        print("  Matched density did not reproduce the real corpus's error.")
        print("  The synthetic rows above are NOT evidence and must not be quoted.")
    print(f"hygiene: {hygiene}")
    print(f"\nWritten to {REPORT.name}")
    return 0


COMMAND = "scale"
USAGE = "scale [--sizes N,N,...]"


def run(sizes: tuple[int, ...] = SIZES) -> int:
    return asyncio.run(_run(sizes))


def cli(argv: list[str]) -> int:
    """`--sizes N,N,...`, or the default ladder."""
    if "--sizes" in argv:
        return run(tuple(int(n) for n in argv[argv.index("--sizes") + 1].split(",")))
    return run()
