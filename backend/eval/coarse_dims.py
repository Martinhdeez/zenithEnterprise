# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
# pyright: reportPrivateUsage=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.
#
# `reportPrivateUsage` is the one addition, and it is the point rather than a concession:
# this file deliberately imports another sweep's underscored helpers instead of copying
# them. A second copy of a corpus generator or of a basis fit would be a second generator
# and a second basis, and two numbers taken against two of those cannot be read beside each
# other -- which is the only use either number has.

"""Do the two levers on the index-size ceiling multiply, or do they interfere?

Two ways of making the resident vector index smaller have been measured, separately, and both
work:

- **Grouping** (`coarse.json`): one resident vector per group of sixteen passages instead of
  one per passage. `chunks16/mean/open32` reproduces the exact dense baseline on all four
  scored quantities at 15.6x fewer resident vectors, reading 3.7% of passages in stage two.
- **Dimensions** (`dimensions.json`): an uncentred SVD basis fitted on this corpus, projecting
  1024 components to 512. Half the bytes per vector, at -2.0 points of index recall@10, with
  the worst question unchanged and **no question worse by more than 0.1**.

Multiplied that is about 31x, which is the difference between a million documents needing 880
GB resident and needing 28 GB. Nobody has measured the two together, and there is a specific
reason to think the product might not be the product: **pooling already destroys information,
and a projection destroys more.** A group's mean vector is a summary of sixteen passages; if
the sixteen are summarised in a subspace that has already discarded half its directions, the
summary may no longer point at the passage that would have answered the question. Or the two
losses may be in different places entirely and compose for nothing. It is not arguable from
first principles either way, which is why it is here.

The projection is fitted by `dimensions.py`'s own `_fit`, imported rather than reimplemented.
Two fits would be two different bases — the eigenvectors are only defined up to sign and up to
the ordering of near-degenerate eigenvalues — and two numbers taken against two bases could
not be read beside each other, which is the only use either has.

## One thing that is free, and worth saying so it is not mistaken for a result

Mean pooling and a linear projection commute exactly: `P(mean(x)) == mean(P(x))`. So a
deployment could project at ingestion and pool the projected vectors, or pool and project the
group vector, and get the same index. This file pools the projected vectors, which is the
order a deployment would use, and the equality is checked rather than assumed. It does **not**
hold for max pooling — which is one more reason `coarse.json`'s max arms are not carried
forward.

## The bar, and where it came from

> Headline Recall@8 must not fall below the exact dense baseline, and Recall@1 must not fall
> below it either.

That is `coarse.json`'s end-to-end bar, unchanged, against a baseline measured by the same
harness in the same run. Index recall is reported too, and only as context: **the reasoning
that dense-stage loss is absorbed by the lexical half was refuted by `coarse.json` itself.**
`chunks16/mean/open16` has *better* dense recall than `binary_r20` (0.8258 against 0.8133) and
*worse* headline recall (0.80 against 0.90), because group pruning discards coherent regions of
the corpus and the lexical half tends to fail on the same questions. The two halves are not
independent. So nothing here is concluded from a dense-stage number; every verdict is read off
the page.

**Read-only.** Everything is built inside `zenith_coarse`, dropped in a `finally`; `chunks`
and `chunk_embeddings` are read and never written.

    docker compose exec -T api python -m eval coarse-dims
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.core.config import settings
from app.core.hardware import active as active_profile
from app.features.embeddings.client import DIMENSION, MODEL, VERSION
from app.features.retrieval.search import CANDIDATES
from eval.coarse import GROUPINGS
from eval.dimensions import _corpus, _fit, _literals, _project
from eval.harness import installation
from eval.quantisation import end_to_end
from eval.questions import load_questions

REPORT = Path(__file__).parent / "coarse-dims.json"
DIMENSIONS = Path(__file__).parent / "dimensions.json"
SCHEMA = "zenith_coarse"

#: The grouping `coarse.json` took to the page and cleared the bar with.
GROUPING = "chunks16"

#: The width `dimensions.json` found unbroken: -2.0 points of index recall, worst question
#: unchanged, no question worse by more than 0.1. Everything narrower lost a question there,
#: so nothing narrower is worth a combined measurement.
WIDTH = 512

#: Recall depth for the index-recall sweep, as in `coarse.json`.
DEPTH = 10

#: `coarse.py`'s own dial ladder, so the two sweeps are readable side by side.
OPEN = (2, 4, 8, 16, 32, 64, 128)

#: Configurations taken through the whole product. The baseline and the two single-lever arms
#: are here as well as the combination, because "did they compose" is a question about three
#: numbers and not one — a combined arm that holds tells a reader nothing if it is not beside
#: what each lever does on its own, measured in the same run on the same machine.
#:
#: `open64` and `open128` at 512 are here for the case the combination is not free at the
#: setting that was free at 1024: the dial exists precisely so a disappointing measurement
#: produces a larger number rather than an abandoned design, and the cost of the larger
#: number is in the report next to it.
END_TO_END: tuple[tuple[str, int, int | None], ...] = (
    ("exact", DIMENSION, None),
    ("flat", WIDTH, None),
    ("grouped", DIMENSION, 32),
    ("grouped", WIDTH, 32),
    ("grouped", WIDTH, 64),
    ("grouped", WIDTH, 128),
)


def _column(dim: int) -> str:
    return "embedding" if dim == DIMENSION else f"e{dim}"


async def _build(conn: AsyncConnection, ids: list[str], projected: list[str]) -> dict[str, Any]:
    """Passages with their group and both widths, and one pooled mean per group per width.

    The grouping expression is `coarse.py`'s, imported. Writing it again would be a second
    grouping that happens to look like the first, and every comparison in this file is against
    numbers `coarse.json` measured over that exact one.

    Both pooled means are `avg()` over the stored column. For the 512-wide one that is the
    mean of the projected passages, which is the order a deployment would use — project at
    ingestion, pool at index time. `commutes` below checks that it is also the other order.
    """
    key = GROUPINGS[GROUPING]
    await conn.execute(
        text(
            f"CREATE TABLE {SCHEMA}.members AS "
            f"SELECT c.id AS chunk_id, {key} AS gid, e.embedding "
            "FROM chunks c JOIN chunk_embeddings e ON e.chunk_id = c.id "
            "WHERE e.embedding_model = :model AND e.embedding_version = :version"
        ),
        {"model": MODEL, "version": VERSION},
    )
    await conn.execute(
        text(f"ALTER TABLE {SCHEMA}.members ADD COLUMN {_column(WIDTH)} vector({WIDTH})")
    )
    batch = 2000
    for start in range(0, len(ids), batch):
        await conn.execute(
            text(
                f"UPDATE {SCHEMA}.members m SET {_column(WIDTH)} = u.v "
                f"FROM unnest(CAST(:i AS uuid[]), CAST(:v AS vector({WIDTH})[])) u(id, v) "
                "WHERE m.chunk_id = u.id"
            ),
            {"i": ids[start : start + batch], "v": projected[start : start + batch]},
        )
    await conn.execute(text(f"CREATE INDEX ON {SCHEMA}.members (gid)"))
    await conn.execute(text(f"CREATE INDEX ON {SCHEMA}.members (chunk_id)"))

    missing = int(
        (
            await conn.execute(
                text(f"SELECT count(*) FROM {SCHEMA}.members WHERE {_column(WIDTH)} IS NULL")
            )
        ).scalar_one()
    )
    if missing:
        raise RuntimeError(f"{missing} passages did not receive a projected vector")

    await conn.execute(
        text(
            f"CREATE TABLE {SCHEMA}.grp AS SELECT gid, count(*)::int AS n, "
            f"avg(embedding)::vector({DIMENSION}) AS pooled_{DIMENSION}, "
            f"avg({_column(WIDTH)})::vector({WIDTH}) AS pooled_{WIDTH} "
            f"FROM {SCHEMA}.members GROUP BY gid"
        )
    )
    await conn.execute(text(f"ALTER TABLE {SCHEMA}.grp ADD PRIMARY KEY (gid)"))
    await conn.execute(text(f"ANALYZE {SCHEMA}.members"))
    await conn.execute(text(f"ANALYZE {SCHEMA}.grp"))
    counted = (
        await conn.execute(
            text(
                f"SELECT count(*), (SELECT count(*) FROM {SCHEMA}.grp), "
                "(SELECT count(DISTINCT document_id) FROM chunks) "
                f"FROM {SCHEMA}.members"
            )
        )
    ).one()
    return {
        "passages": int(counted[0]),
        "groups": int(counted[1]),
        "documents": int(counted[2]),
        "passages_per_document": round(int(counted[0]) / int(counted[2]), 1),
    }


async def _commutes(conn: AsyncConnection, basis: Any, mean: Any) -> dict[str, Any]:
    """`P(mean(x))` against `mean(P(x))`, on the group vectors this run will actually use.

    The claim is exact arithmetic and the check is therefore about fp32 accumulation rather
    than about the algebra, which is why the tolerance is loose and the number is reported.
    A large difference here would mean the projection is not linear as applied — a centred
    family, say — and `dimensions.json` records that centring is the single largest loss in
    that report.
    """
    import numpy as np

    rows = (
        await conn.execute(
            text(
                f"SELECT pooled_{DIMENSION}::text AS wide, pooled_{WIDTH}::text AS narrow "
                f"FROM {SCHEMA}.grp ORDER BY gid"
            )
        )
    ).all()
    wide = np.asarray(
        [np.fromstring(row.wide[1:-1], sep=",", dtype=np.float32) for row in rows],
        dtype=np.float32,
    )
    narrow = np.asarray(
        [np.fromstring(row.narrow[1:-1], sep=",", dtype=np.float32) for row in rows],
        dtype=np.float32,
    )
    difference = float(np.abs(_project(wide, mean, basis, WIDTH, False) - narrow).max())
    scale = float(np.abs(narrow).max())
    return {
        "groups": len(rows),
        "max_abs_difference": float(f"{difference:.3g}"),
        "relative_to_largest_component": float(f"{difference / scale:.3g}"),
        "commutes": difference / scale < 1e-3,
    }


async def _truth(conn: AsyncConnection, queries: list[str]) -> list[set[str]]:
    """Exact fp32 neighbours at 1024 dimensions, every index refused.

    The reference for the index-recall sweep, and deliberately the *unprojected* one: what is
    being asked is what the projection and the grouping together cost against retrieval as it
    is today, not against a reduced index's own idea of the answer.
    """
    await conn.execute(text("SET LOCAL enable_indexscan = off"))
    await conn.execute(text("SET LOCAL enable_bitmapscan = off"))
    out: list[set[str]] = []
    for vector in queries:
        rows = await conn.execute(
            text(
                f"SELECT chunk_id::text AS id FROM {SCHEMA}.members "
                "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
            ),
            {"q": vector, "k": DEPTH},
        )
        out.append({row.id for row in rows})
    return out


async def _sweep(
    conn: AsyncConnection,
    queries: list[str],
    truth: list[set[str]],
    dim: int,
    total: int,
) -> list[dict[str, Any]]:
    """Coarse then fine at every dial position, at one width. Exact at both stages."""
    await conn.execute(text("SET LOCAL enable_indexscan = off"))
    await conn.execute(text("SET LOCAL enable_bitmapscan = off"))
    column = _column(dim)
    arms: list[dict[str, Any]] = []
    for opened in OPEN:
        hits: list[float] = []
        scanned: list[int] = []
        for index, vector in enumerate(queries):
            rows = (
                await conn.execute(
                    text(
                        f"WITH picked AS (SELECT gid, n FROM {SCHEMA}.grp "
                        f"  ORDER BY pooled_{dim} <=> CAST(:q AS vector({dim})) LIMIT :p) "
                        "SELECT m.chunk_id::text AS id, (SELECT sum(n) FROM picked) AS seen "
                        f"FROM {SCHEMA}.members m JOIN picked ON picked.gid = m.gid "
                        f"ORDER BY m.{column} <=> CAST(:q AS vector({dim})) LIMIT :k"
                    ),
                    {"q": vector, "p": opened, "k": DEPTH},
                )
            ).all()
            found = {row.id for row in rows}
            scanned.append(int(rows[0].seen) if rows else 0)
            hits.append(len(truth[index] & found) / DEPTH)
        arms.append(
            {
                "dim": dim,
                "open": opened,
                "recall_at_10": round(sum(hits) / len(hits), 4),
                "worst_question": round(min(hits), 4),
                "io_fraction": round(sum(scanned) / len(scanned) / total, 4),
            }
        )
    return arms


def _dense_stage(dim: int, opened: int | None, project: Any) -> Any:
    """The dense half as this arm defines it, in the shape `end_to_end` expects.

    Exact at every stage and every index refused, as in `coarse.py`: what is being asked is
    what the *grouping* and the *projection* cost, and an approximate index on top would fold
    a third error into the same number with none of the three recoverable from it.
    """
    column = _column(dim)

    async def stage(scratch: AsyncConnection, embedding: list[float]) -> list[UUID]:
        await scratch.execute(text("SET LOCAL enable_indexscan = off"))
        await scratch.execute(text("SET LOCAL enable_bitmapscan = off"))
        vector = project(embedding)
        if opened is None:
            rows = await scratch.execute(
                text(
                    f"SELECT chunk_id FROM {SCHEMA}.members "
                    f"ORDER BY {column} <=> CAST(:q AS vector({dim})) LIMIT :k"
                ),
                {"q": vector, "k": CANDIDATES},
            )
        else:
            rows = await scratch.execute(
                text(
                    f"WITH picked AS (SELECT gid FROM {SCHEMA}.grp "
                    f"  ORDER BY pooled_{dim} <=> CAST(:q AS vector({dim})) LIMIT :p) "
                    f"SELECT m.chunk_id FROM {SCHEMA}.members m JOIN picked ON picked.gid = m.gid "
                    f"ORDER BY m.{column} <=> CAST(:q AS vector({dim})) LIMIT :k"
                ),
                {"q": vector, "p": opened, "k": CANDIDATES},
            )
        return [row.chunk_id for row in rows]

    return stage


def _memory(groups: int, passages: int, per_document: float) -> dict[str, Any]:
    """The combined resident factor, from bytes `dimensions.json` measured rather than assumed.

    Grouping changes how many vectors are resident; the width changes what each one costs.
    Neither number is restated here — the bytes come out of `dimensions.json`'s freshly built
    arms, which is where the only measurement of them lives.
    """
    if not DIMENSIONS.exists():
        return {"available": False, "reason": "dimensions.json not present"}
    report = json.loads(DIMENSIONS.read_text())
    per_vector = report["bytes_per_vector"]
    wide = float(per_vector[f"identity_{DIMENSION}"])
    narrow = float(per_vector[f"svd_{WIDTH}"])
    today = passages * wide
    return {
        "source": "dimensions.json bytes_per_vector (fresh HNSW at m=16, ef_construction=64)",
        "bytes_per_vector": {f"identity_{DIMENSION}": wide, f"svd_{WIDTH}": narrow},
        "resident_vectors": {"today": passages, "grouped": groups},
        "factor": {
            f"width_only_svd_{WIDTH}": round(wide / narrow, 2),
            f"grouping_only_{GROUPING}": round(passages / groups, 2),
            "combined": round(today / (groups * narrow), 2),
        },
        "gib_at_1m_documents": {
            "passages_per_document": per_document,
            "today": round(1_000_000 * per_document * wide / 1024**3, 1),
            "combined": round(1_000_000 * per_document / (passages / groups) * narrow / 1024**3, 1),
        },
        "note": (
            "Resident index only. The passage vectors stage two rescans still exist on disk "
            "at full width — grouping moves them out of memory, it does not delete them — and "
            "at 512 components they are half the disk they are today."
        ),
    }


def _verdict(reached: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The bar, applied. `coarse.json`'s, unchanged, against this run's own baseline."""
    baseline = reached["exact"]
    out: dict[str, Any] = {}
    for key, arm in reached.items():
        if key == "exact":
            continue
        headline = arm["headline_recall_at_8"] - baseline["headline_recall_at_8"]
        top1 = arm["recall_at_1_all"] - baseline["recall_at_1_all"]
        out[key] = {
            "headline_delta": round(headline, 4),
            "recall_at_1_delta": round(top1, 4),
            "recall_at_8_all_delta": round(arm["recall_at_8_all"] - baseline["recall_at_8_all"], 4),
            "clears_bar": headline >= 0 and top1 >= 0,
        }
    return out


async def _hygiene(conn: AsyncConnection) -> dict[str, Any]:
    mine = (SCHEMA, "zenith_scale")
    schemas = [
        row[0]
        for row in await conn.execute(
            text("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'zenith%'")
        )
    ]
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
    left = [name for name in schemas if name in mine]
    return {
        "own_schemas_left": left,
        "other_scratch_schemas_seen": [name for name in schemas if name not in mine],
        "security_definer_in_public": definers,
        "clean": not left and definers == 7,
    }


async def _run() -> int:
    try:
        import numpy as np
    except ModuleNotFoundError:
        print(
            "numpy is required and is not installed in this container.\n"
            "  uv pip install --python /app/.venv/bin/python 'numpy>=2.1'"
        )
        return 1

    started = time.perf_counter()
    where = await installation()
    profile = active_profile()
    loaded = load_questions()
    answerable = [q for q in loaded if q.sources]
    print(
        f"{len(where.questions)} questions scored end to end, "
        f"{len(answerable)} answerable for the index sweep, profile {profile.name}"
    )

    engine = create_async_engine(settings.database_owner_url)
    reached: dict[str, dict[str, Any]] = {}
    sweeps: list[dict[str, Any]] = []
    built: dict[str, Any] = {}
    commutation: dict[str, Any] = {}

    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
            await conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
            await conn.execute(text("SET LOCAL max_parallel_workers_per_gather = 0"))

            ids, matrix = await _corpus(conn, where.space)
            fit_started = time.perf_counter()
            mean, bases, spectra = _fit(matrix)
            basis = bases["svd"]
            print(
                f"  {len(ids)} vectors, svd basis fitted in "
                f"{time.perf_counter() - fit_started:.1f} s, "
                f"{spectra['svd'][WIDTH - 1]:.4f} of the spectrum kept at {WIDTH}",
                flush=True,
            )
            built = await _build(conn, ids, _literals(_project(matrix, mean, basis, WIDTH, False)))
            print(f"  built {built}", flush=True)
            commutation = await _commutes(conn, basis, mean)
            print(f"  pooling commutes with the projection: {commutation}", flush=True)

        from app.features.embeddings.client import TeiClient

        embedder = TeiClient(profile=profile)
        embedded = await embedder.embed([q.question for q in loaded])
        keep = [i for i, q in enumerate(loaded) if q.sources]
        wide_queries = [str([float(x) for x in embedded[i]]) for i in keep]
        kept = np.asarray([embedded[i] for i in keep], dtype=np.float32)
        narrow_queries = _literals(_project(kept, mean, basis, WIDTH, False))

        async with engine.connect() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            truth = await _truth(conn, wide_queries)
            for dim, queries in ((DIMENSION, wide_queries), (WIDTH, narrow_queries)):
                arms = await _sweep(conn, queries, truth, dim, built["passages"])
                sweeps.extend(arms)
                for arm in arms:
                    print(
                        f"    dim {arm['dim']:>4} open {arm['open']:>4}  "
                        f"recall {arm['recall_at_10']:.4f}  worst {arm['worst_question']:.2f}  "
                        f"reads {arm['io_fraction']:.1%}",
                        flush=True,
                    )
            await conn.rollback()

        def projector(dim: int) -> Any:
            def unprojected(embedding: list[float]) -> str:
                return str([float(x) for x in embedding])

            if dim == DIMENSION:
                return unprojected

            def project_one(embedding: list[float]) -> str:
                return _literals(
                    _project(np.asarray([embedding], dtype=np.float32), mean, basis, WIDTH, False)
                )[0]

            return project_one

        print()
        for shape, dim, opened in END_TO_END:
            key = (
                f"exact_{dim}"
                if shape == "exact"
                else f"svd{dim}_flat"
                if shape == "flat"
                else f"{GROUPING}/mean/open{opened}@{dim}"
            )
            if shape == "exact":
                key = "exact"
            reached[key] = await end_to_end(
                where, _dense_stage(dim, opened, projector(dim)), profile.hnsw_ef_search
            )
            print(f"  {key:<28} {json.dumps(reached[key])}", flush=True)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        async with engine.connect() as conn:
            hygiene = await _hygiene(conn)
            await conn.rollback()
        await engine.dispose()

    verdict = _verdict(reached)
    REPORT.write_text(
        json.dumps(
            {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "took_s": round(time.perf_counter() - started, 1),
                "question": (
                    "Grouping and dimension reduction were measured separately and both work. "
                    "Do they compose end to end, or does pooling a projected vector lose what "
                    "pooling an unprojected one kept?"
                ),
                "corpus": built,
                "grouping": f"{GROUPING}/mean",
                "width": WIDTH,
                "projection": (
                    "Uncentred SVD basis fitted by eval/dimensions.py's `_fit`, imported. Two "
                    "fits would be two bases — eigenvectors are defined up to sign and up to "
                    "the ordering of near-degenerate eigenvalues — and numbers taken against "
                    "two bases cannot be read beside each other."
                ),
                "commutation": commutation,
                "questions": {
                    "end_to_end_scored": len(where.questions),
                    "index_sweep_scored": len(answerable),
                },
                "depth": DEPTH,
                "index_recall": {
                    "truth": (
                        f"Exact fp32 cosine at {DIMENSION} components over the passages, "
                        "enable_indexscan and enable_bitmapscan off. Unprojected on purpose: "
                        "the question is what the two levers cost against retrieval as it is "
                        "today."
                    ),
                    "arms": sweeps,
                },
                "end_to_end": reached,
                "bar": (
                    "headline Recall@8 and Recall@1 must not fall below the exact dense "
                    "baseline measured in this run. coarse.json's bar, unchanged."
                ),
                "verdict": verdict,
                "memory": _memory(
                    built["groups"], built["passages"], built["passages_per_document"]
                ),
                "caveats": [
                    "Dense-stage recall does not predict the page and is reported only as "
                    "context. coarse.json refuted that inference with its own numbers: "
                    "chunks16/mean/open16 has better dense recall than binary_r20 and worse "
                    "headline recall, because group pruning discards coherent regions and the "
                    "lexical half tends to fail on the same questions.",
                    "Every arm is exact at both stages, with all indexes refused, so this "
                    "measures what the grouping and the projection cost and not what HNSW "
                    "costs on top. dimensions.json measured the arms under HNSW and found "
                    "identity_1024 itself at 0.9667 against exact — a real installation pays "
                    "that before either lever is applied.",
                    "The SVD basis is fitted on 42 documents. It is a statement about this "
                    "corpus's structure, and a basis fitted on a thousand tenants' documents "
                    "may not be this one. dimensions.json makes the same caveat and it is not "
                    "weakened by being repeated.",
                    "Shipping a fitted basis means storing a 1024x512 matrix, applying it in "
                    "the ingestion and the query path, and deciding what happens when the "
                    "corpus drifts far enough that the fit is stale. None of that is measured "
                    "here.",
                    "The operating point measured here is the one that was free at 13,549 "
                    "passages. Whether `open` can stay at 32 as the corpus grows is a "
                    "different question, measured in coarse-scale.json, and this file's "
                    "verdict is conditional on that one.",
                ],
                "hygiene": hygiene,
            },
            indent=2,
        )
        + "\n"
    )

    print(
        f"\nbar: {reached['exact']['headline_recall_at_8']:.4f} headline / "
        f"{reached['exact']['recall_at_1_all']:.4f} at one (the exact baseline)"
    )
    for key, entry in verdict.items():
        print(
            f"  {key:<28} headline {entry['headline_delta']:+.4f}  "
            f"at-one {entry['recall_at_1_delta']:+.4f}  "
            f"{'CLEARS' if entry['clears_bar'] else 'FAILS'}"
        )
    print(f"\nhygiene: {hygiene}")
    print(f"Written to {REPORT.name}")
    return 0 if hygiene["clean"] else 1


COMMAND = "coarse-dims"
USAGE = "coarse-dims"


def run() -> int:
    return asyncio.run(_run())
