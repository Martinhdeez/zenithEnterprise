# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""Can the dense half be coarse, and what does coarse cost?

Every attempt on the index-size ceiling so far has shrunk the *vector*: fp32 to fp16 (three
times, adopted), fp16 to one bit per dimension (nineteen times, degrades). All of them keep
**one resident vector per passage**, and that is the quantity that makes a million documents
need hundreds of gigabytes. At 322 passages per document even the smallest vector measured
leaves 140 GB resident.

This measures the other axis. Group passages, keep one vector per *group* resident, and read
the passage vectors from disk only for the groups the coarse stage selects. Resident memory
then scales with groups rather than with passages — sixteen times fewer at a group of
sixteen, three hundred times fewer at a group per document.

## Why this is a dial and quantisation was a cliff

Binary quantisation destroys information when the index is written. If it turns out to have
lost too much there is nothing to turn up; the bits are gone, and that is why
`scale.json` ended in a rejection rather than a setting.

Nothing is destroyed here. **Every passage vector remains exactly as it is today.** The only
question is how many groups the first stage opens, and opening all of them reproduces
today's behaviour identically. Today's behaviour sits at one end of the dial, which means a
disappointing measurement produces a larger number rather than an abandoned design — and the
cost of the larger number is reading a few more megabytes from an SSD.

## The failure this is looking for

A question whose answer is one passage inside a group that is otherwise about something
else. The group's vector does not reflect it, the group is not opened, the passage never
reaches the page.

Which is why **pooling is measured rather than chosen**. A mean over sixteen passages
dilutes exactly that passage — fifteen unrelated neighbours drag the group's vector away
from it. A per-dimension maximum keeps the strongest signal present anywhere in the group,
so one passage that matches strongly can still make its group stand out. That is the
argument; the numbers decide whether it survives contact with a real corpus.

Two things already in the product contain the same failure, and neither is touched here: the
lexical half indexes every passage at full granularity and lives on disk by design, so a
passage found by its words is unaffected; and the cross-encoder reranks real passages, so
precision is recovered from whatever the candidate set turns out to be.

## What is reported, and the bar set before the run

For each grouping and pooling, the smallest number of opened groups at which recall@10
against **exact** retrieval is unbroken, together with what that costs: the fraction of
passage vectors that stage two has to read.

The bar is stated here so the result cannot be read backwards afterwards: **recall@10 must
reach 1.0000, and the worst single question must reach 1.0, before a configuration counts as
free.** A configuration that saves memory and loses a question is not a smaller index, it is
a worse product — the asymmetry F26 was built on, applied to a different mechanism.

**Read-only.** Everything is built inside `zenith_coarse`, dropped in a `finally`;
`chunk_embeddings` and `chunks` are read and never written.

    docker compose exec -T api python -m eval coarse
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.core.config import settings
from app.core.hardware import active as active_profile
from app.features.embeddings.client import MODEL, VERSION, TeiClient
from app.features.retrieval.search import CANDIDATES
from eval.harness import installation
from eval.quantisation import end_to_end
from eval.questions import load_questions

REPORT = Path(__file__).parent / "coarse.json"
SCHEMA = "zenith_coarse"

#: How passages are gathered into a group, and the SQL that assigns each passage its group.
#:
#: `chunks16` is the interesting one: sixteen consecutive passages of the same document, in
#: reading order. `page` and `document` are the groupings the schema already has, included
#: because a grouping that needs no new column is cheaper to ship than one that does.
GROUPINGS: dict[str, str] = {
    "chunks16": (
        "(c.document_id::text || ':' || "
        "((row_number() OVER (PARTITION BY c.document_id ORDER BY c.page_num, c.char_start)"
        " - 1) / 16)::text)"
    ),
    "chunks64": (
        "(c.document_id::text || ':' || "
        "((row_number() OVER (PARTITION BY c.document_id ORDER BY c.page_num, c.char_start)"
        " - 1) / 64)::text)"
    ),
    # `#` rather than `:p`: `text()` reads `:name` as a bind parameter anywhere in the
    # string, including inside a SQL string literal, and `':p'` became a missing parameter.
    # `coalesce`: `chunks.page_num` is nullable since migration 0021 (text documents
    # have no pages), and a NULL key groups every such passage together and then fails
    # the primary key. `#none` keeps them one group per document, as the key intends.
    "page": "(c.document_id::text || '#' || coalesce(c.page_num::text, 'none'))",
    "document": "c.document_id::text",
}

#: Mean is `avg(vector)`, which pgvector provides. Max has no aggregate, so it goes through
#: `real[]` — expensive to build and irrelevant to query cost, since it happens once at
#: index time.
POOLINGS = ("mean", "max")

#: The dial. How many groups the coarse stage hands to the fine stage.
OPEN = (2, 4, 8, 16, 32, 64, 128)

#: Ten neighbours, as in `quantisation.json` and `scale.json`, so all three are readable
#: beside each other.
DEPTH = 10

#: Which configurations are also run through the whole product. Chosen after the index-recall
#: sweep of the first run, and the reason is recorded rather than hidden: **no configuration
#: met this file's original bar**, which was recall@10 of the dense stage alone.
#:
#: That bar was set on the wrong quantity, and the evidence for saying so predates this
#: harness. `quantisation.json` measured `binary_r20` at a dense-stage index recall of
#: **0.8133** and it reached the page at headline Recall@8 0.90 — identical to fp32, with
#: only Recall@1 moving by one question in thirty. The lexical half and the cross-encoder
#: absorb dense-stage loss, and this repository already treats the page as what decides.
#:
#: Adding a measurement after an unfavourable result is exactly when a reader should be
#: suspicious, so the original bar stays in this file, failed, rather than being rewritten.
#: The new one is stated before the run: **headline Recall@8 must not drop and Recall@1 must
#: not drop.** Both against the exact dense baseline measured by the same harness.
END_TO_END: tuple[tuple[str, str, int], ...] = (
    ("chunks16", "mean", 8),
    ("chunks16", "mean", 16),
    ("chunks16", "mean", 32),
    ("chunks16", "mean", 64),
    ("chunks64", "mean", 16),
    ("chunks64", "mean", 32),
)


@dataclass(frozen=True, slots=True)
class Arm:
    grouping: str
    pooling: str
    groups: int
    ram_factor: float
    opened: int
    recall: float
    worst: float
    io_fraction: float
    median_ms: float


async def _build_groups(conn: AsyncConnection, grouping: str) -> int:
    """One row per group, carrying both pooled vectors."""
    key = GROUPINGS[grouping]
    await conn.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.members CASCADE"))
    await conn.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.grp CASCADE"))
    await conn.execute(
        text(
            f"CREATE TABLE {SCHEMA}.members AS SELECT c.id AS chunk_id, {key} AS gid, "
            "e.embedding FROM chunks c JOIN chunk_embeddings e ON e.chunk_id = c.id "
            "WHERE e.embedding_model = :model AND e.embedding_version = :version"
        ),
        {"model": MODEL, "version": VERSION},
    )
    await conn.execute(text(f"CREATE INDEX ON {SCHEMA}.members (gid)"))

    # Mean is an aggregate pgvector ships. Max is not, so each dimension is maxed through
    # `real[]` with ordinality to keep the positions aligned — slow, and it runs once.
    await conn.execute(
        text(
            f"CREATE TABLE {SCHEMA}.grp AS "
            "SELECT m.gid, count(*)::int AS n, "
            "  avg(m.embedding)::vector(1024) AS pooled_mean, "
            "  (SELECT array_agg(mx ORDER BY i)::vector(1024) FROM ("
            "     SELECT u.i, max(u.v) AS mx FROM "
            f"      {SCHEMA}.members m2, unnest(m2.embedding::real[]) WITH ORDINALITY u(v, i) "
            "     WHERE m2.gid = m.gid GROUP BY u.i) z) AS pooled_max "
            f"FROM {SCHEMA}.members m GROUP BY m.gid"
        )
    )
    await conn.execute(text(f"ALTER TABLE {SCHEMA}.grp ADD PRIMARY KEY (gid)"))
    await conn.execute(text(f"ANALYZE {SCHEMA}.grp"))
    return int((await conn.execute(text(f"SELECT count(*) FROM {SCHEMA}.grp"))).scalar_one())


async def _truth(conn: AsyncConnection, queries: list[list[float]]) -> list[set[str]]:
    """Exact nearest passages over the whole corpus, every index refused.

    The reference every arm is scored against. An approximate reference would report the
    difference between two errors rather than the cost of one.
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
            {"q": str(vector), "k": DEPTH},
        )
        out.append({row.id for row in rows})
    return out


async def _measure(
    conn: AsyncConnection,
    queries: list[list[float]],
    truth: list[set[str]],
    pooling: str,
    opened: int,
    keep: list[int],
    total_chunks: int,
) -> tuple[float, float, float, float]:
    """Coarse then fine, at one dial position. Returns recall, worst, io fraction, median ms."""
    await conn.execute(text("SET LOCAL enable_indexscan = off"))
    await conn.execute(text("SET LOCAL enable_bitmapscan = off"))
    column = f"pooled_{pooling}"

    hits: list[float] = []
    scanned: list[int] = []
    times: list[float] = []

    for index in keep:
        vector = queries[index]
        started = time.perf_counter()
        rows = await conn.execute(
            text(
                f"WITH picked AS (SELECT gid, n FROM {SCHEMA}.grp "
                f"  ORDER BY {column} <=> CAST(:q AS vector) LIMIT :p) "
                f"SELECT m.chunk_id::text AS id, (SELECT sum(n) FROM picked) AS seen "
                f"FROM {SCHEMA}.members m JOIN picked ON picked.gid = m.gid "
                "ORDER BY m.embedding <=> CAST(:q AS vector) LIMIT :k"
            ),
            {"q": str(vector), "p": opened, "k": DEPTH},
        )
        got = rows.all()
        times.append((time.perf_counter() - started) * 1000)
        found = {row.id for row in got}
        scanned.append(int(got[0].seen) if got else 0)
        hits.append(len(truth[index] & found) / DEPTH)

    ordered = sorted(times)
    return (
        round(sum(hits) / len(hits), 4),
        round(min(hits), 4),
        round(sum(scanned) / len(scanned) / total_chunks, 4),
        round(ordered[len(ordered) // 2], 2),
    )


def _coarse_dense(pooling: str, opened: int):
    """The dense half as coarse-then-fine, in the shape `_end_to_end` expects.

    Exact at both stages and every index refused, deliberately. What is being asked is what
    the *grouping* costs; adding an approximate index on top would fold two errors into one
    number and neither would be recoverable from it.
    """
    column = f"pooled_{pooling}"

    async def stage(scratch: AsyncConnection, embedding: list[float]) -> list[UUID]:
        await scratch.execute(text("SET LOCAL enable_indexscan = off"))
        await scratch.execute(text("SET LOCAL enable_bitmapscan = off"))
        rows = await scratch.execute(
            text(
                f"WITH picked AS (SELECT gid FROM {SCHEMA}.grp "
                f"  ORDER BY {column} <=> CAST(:q AS vector) LIMIT :p) "
                f"SELECT m.chunk_id FROM {SCHEMA}.members m JOIN picked ON picked.gid = m.gid "
                "ORDER BY m.embedding <=> CAST(:q AS vector) LIMIT :k"
            ),
            {"q": str(embedding), "p": opened, "k": CANDIDATES},
        )
        return [row.chunk_id for row in rows]

    return stage


async def _exact_dense(scratch: AsyncConnection, embedding: list[float]) -> list[UUID]:
    """Today's dense half, without its index: the reference the bar is set against."""
    await scratch.execute(text("SET LOCAL enable_indexscan = off"))
    await scratch.execute(text("SET LOCAL enable_bitmapscan = off"))
    rows = await scratch.execute(
        text(
            f"SELECT chunk_id FROM {SCHEMA}.members "
            "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
        ),
        {"q": str(embedding), "k": CANDIDATES},
    )
    return [row.chunk_id for row in rows]


async def _run() -> int:
    engine = create_async_engine(settings.database_owner_url)
    started = time.perf_counter()

    loaded = load_questions()
    queries = await TeiClient(profile=active_profile()).embed([q.question for q in loaded])
    answerable = [i for i, q in enumerate(loaded) if q.sources]
    print(f"{len(answerable)} answerable of {len(loaded)} questions\n")

    arms: list[Arm] = []
    reached: dict[str, object] = {}
    where = await installation()
    profile = active_profile()
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
            await conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))

        for grouping in GROUPINGS:
            async with engine.begin() as conn:
                await conn.execute(text("SET LOCAL max_parallel_workers_per_gather = 0"))
                groups = await _build_groups(conn, grouping)
            async with engine.connect() as conn:
                await conn.execute(text("SET TRANSACTION READ ONLY"))
                total = int(
                    (
                        await conn.execute(text(f"SELECT count(*) FROM {SCHEMA}.members"))
                    ).scalar_one()
                )
                truth = await _truth(conn, queries)
                ram = round(total / groups, 1)
                print(f"{grouping}: {groups} groups over {total} passages  ->  {ram}x fewer")

                for pooling in POOLINGS:
                    for opened in OPEN:
                        if opened > groups:
                            continue
                        recall, worst, io, ms = await _measure(
                            conn, queries, truth, pooling, opened, answerable, total
                        )
                        arms.append(
                            Arm(grouping, pooling, groups, ram, opened, recall, worst, io, ms)
                        )
                        print(
                            f"  {pooling:<5} open {opened:>4}  recall {recall:.4f}"
                            f"  worst {worst:.2f}  reads {io:.1%} of passages  {ms:>7.1f} ms",
                            flush=True,
                        )
                        if recall >= 1.0 and worst >= 1.0:
                            break

            wanted = [c for c in END_TO_END if c[0] == grouping]
            if wanted:
                if "exact" not in reached:
                    reached["exact"] = await end_to_end(where, _exact_dense, profile.hnsw_ef_search)
                    print(f"  {'exact (today)':<22} {json.dumps(reached['exact'])}", flush=True)
                for _, pooling, opened in wanted:
                    key = f"{grouping}/{pooling}/open{opened}"
                    reached[key] = await end_to_end(
                        where, _coarse_dense(pooling, opened), profile.hnsw_ef_search
                    )
                    print(f"  {key:<22} {json.dumps(reached[key])}", flush=True)
            print(flush=True)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        async with engine.connect() as conn:
            left = int(
                (
                    await conn.execute(
                        text("SELECT count(*) FROM pg_namespace WHERE nspname = :s"),
                        {"s": SCHEMA},
                    )
                ).scalar_one()
            )
        await engine.dispose()

    # The bar, applied rather than described: the smallest dial position at which nothing is
    # lost, per grouping and pooling. `None` where no position tried reached it.
    free: dict[str, object] = {}
    for grouping in GROUPINGS:
        for pooling in POOLINGS:
            candidates = [
                a
                for a in arms
                if a.grouping == grouping
                and a.pooling == pooling
                and a.recall >= 1.0
                and a.worst >= 1.0
            ]
            best = min(candidates, key=lambda a: a.opened) if candidates else None
            free[f"{grouping}/{pooling}"] = (
                {
                    "opened": best.opened,
                    "ram_factor": best.ram_factor,
                    "io_fraction": best.io_fraction,
                    "median_ms": best.median_ms,
                }
                if best
                else None
            )

    REPORT.write_text(
        json.dumps(
            {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "took_s": round(time.perf_counter() - started, 1),
                "depth": DEPTH,
                "questions_scored": len(answerable),
                "bar": "recall@10 == 1.0000 and worst question == 1.0",
                "free_at": free,
                "arms": [asdict(a) for a in arms],
                "end_to_end": reached,
                "end_to_end_bar": (
                    "headline Recall@8 and Recall@1 must not fall below the exact baseline"
                ),
                "scratch_schemas_left": left,
            },
            indent=2,
        )
        + "\n"
    )
    print("smallest dial position that loses nothing:")
    for key, value in free.items():
        print(f"  {key:<20} {value}")
    print(f"\nscratch schemas left: {left}\nWritten to {REPORT.name}")
    return 0


COMMAND = "coarse"
USAGE = "coarse"


def run() -> int:
    return asyncio.run(_run())
