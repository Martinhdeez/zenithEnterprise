# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""Whether the vector index has to be in RAM at all.

`quantisation.py` asks how many bytes a vector costs. This asks a question underneath it:
how many of those bytes have to be *resident*. They are not the same number, and only one
of them is the ceiling.

A million documents is 322 passages each at this corpus's measured rate, so ~322M vectors,
and HNSW costs 2,729.9 bytes per vector for `halfvec(1024)` — `hnsw.fp16.bytes_per_vector`
below, reproducing `quantisation.json` to the tenth of a byte from an independent build — which
is 880 GB of graph. Every published deployment keeps that graph in memory, and the reason is
structural rather than habitual: **an HNSW search is a pointer chase.** It enters at the top
layer and walks to a neighbour, then to a neighbour of that neighbour, and the address of
each hop is only known once the previous hop has been read. Every hop is a random read. At
roughly 80 µs for a random 8 KB NVMe read, a few hundred hops is tens of milliseconds, so the
graph goes in RAM and the RAM is the ceiling.

**IVFFlat has a different shape and therefore a different ceiling.** It partitions the
vectors into `lists` clusters, stores one centroid per cluster, and a query compares the
question against every centroid, picks the `probes` nearest, and reads those lists *whole*.
Whole lists are contiguous pages: sequential reads, which an NVMe serves one to two orders
of magnitude more cheaply per byte than random ones. What a query must have in memory is the
centroid table. The vectors themselves can live on disk.

So the question this sweep exists to answer is not "is IVF faster" — on this machine, with
13,549 vectors and everything in page cache, that question is unanswerable. It is:

    at what recall, and at what fraction of the corpus read per query, does IVF match HNSW,
    and how much smaller is the part that has to stay resident?

## What would make IVF worth having, stated before the run

IVF is interesting only if it reaches HNSW's recall while reading a *small* fraction of the
corpus. If matching HNSW needs so many probes that a query reads most of the vectors, then
nothing was saved: the memory removed from the index comes straight back as page cache, and
the working set is the corpus again. Both outcomes are reported here as numbers rather than
as a verdict — `probes / lists` is the nominal fraction, and `shared_blocks` from
`EXPLAIN (ANALYZE, BUFFERS)` is what the query actually touched.

## What is resident, and how it is derived rather than assumed

For HNSW the answer is the whole index: the graph is the thing being walked, there is no
smaller subset that a traversal stays inside, and a partially resident graph pays the random
read on the missing part. `resident_bytes` for HNSW is `index_bytes`, and that identity is
the finding, not an approximation.

For IVF the resident part is the centroid table, and its size is *measured*, not modelled.
pgvector lays an ivfflat index out as one metapage, then the list descriptors — each holding
two block numbers and one centroid — then the entry pages. Building the same index over an
**empty** copy of the table isolates everything that does not depend on the vectors, so
`empty_index_bytes` is the metapage plus the centroid pages plus one blank start page per
list. The count of centroids that fit in an 8 KB page is then derived from the *slope* of
that measurement across two `lists` values — `d(pages)/d(lists) = 1 + 1/centroids_per_page`
— which needs no knowledge of pgvector's struct layout at all, and the two derivations are
cross-checked against each other in `geometry.agrees`.

The result is worth stating in advance because it is unintuitive: a `vector(1024)` centroid
is 4,112 bytes and **two of them do not fit in a page**, so fp32 costs a whole 8 KB page per
list. `halfvec` fits three, `bit` fits fifty-five. The resident footprint therefore does not
scale with the representation the way the index size does.

## Three things this measures that a naive comparison would get wrong

**1. The index must actually be used.** `SET LOCAL enable_indexscan = off` lasts until the
transaction ends, not until the statement does, so a sweep that computes exact ground truth
and then measures an approximate index in the same transaction measures two sequential scans
against each other and reports recall 1.0000 for an index it never opened. That is not a
hypothetical: `quantisation.py` did it, this sweep's first run reproduced it, and the two were
fixed independently in the same batch of work — so `quantisation.json` no longer shows it and
the trap is recorded here rather than pointed at. Ground truth runs in its own transaction,
and every approximate measurement records the plan node that served it: `used_index` is in the
report for each point and a `false` there invalidates that point.

The same trap has a second door. At 13,549 vectors the corpus is small enough that, once a
setting asks the index for a large enough share of it, the planner *correctly* prefers a
sequential scan — `hnsw.ef_search = 10` and `ivfflat.probes = 5 of 10 lists` both did on the
first run of this sweep, and both reported recall exactly 1.0000. So every measured point
here runs with `enable_seqscan = off`, and what the planner would have chosen if left alone
is kept as `planner_prefers_index` rather than thrown away: at the sizes this report is about
a scan is not an option, and at this size it often is.

**A third: an IVF build can refuse.** pgvector's k-means holds a sample of about fifty vectors
per list in `maintenance_work_mem` and raises rather than spilling, so `lists = 3000` over
`vector(1024)` asks for 812 MB and simply will not build below it. A refusal is recorded as
`build_wall` with the memory it wanted, because the memory an IVF build needs is linear in
`lists` and in the width of the representation and that is a wall at 322M vectors, not a
nuisance here.

**2. The unanswerable questions are not scored in the headline.** `questions.toml` holds 43
questions, 12 of which the corpus cannot answer. Their exact top ten is whatever the
embedding space happens to put nearest to a question about nothing, and asking an index to
reproduce that ordering measures the faithful reproduction of noise. Scored together they
inflated an earlier report by about 60%. The 31 answerable ones are the headline — minus any
whose source document is not loaded in this installation, which `installation()` drops by the
same rule `live.py` uses, so the scored count is in the report rather than assumed. The 12
are reported beside them under `unanswerable`, because a large difference between the two is
itself informative about where an approximate index loses.

**3. The worst question, not only the mean.** A mean recall of 0.95 over 31 questions hides a
question at 0.2, and one question returning nothing useful is what a reader experiences. That
statistic is what nearly decided the binary-quantisation verdict wrongly, and it is reported
at every point here.

## Latency is indicative and nothing more

This machine gives the whole Docker VM 7.75 GB and Postgres 128 MB of `shared_buffers`, so
neither index is cached the way a deployment would cache it; two other measurement agents are
competing for the same CPUs; and at 13,549 vectors the whole corpus fits in a scan short
enough that no index has to be good. **Recall, index size and resident footprint are the
numbers this report is for.** Latency is recorded because leaving it out would invite someone to
measure it worse, and it is caveated everywhere it appears.

## Hygiene

Everything is built in one schema, `zenith_ivf`, dropped in a `finally`, and the run prints
its own verification that neither the schema nor any new `SECURITY DEFINER` function in
`public` survived it. `chunk_embeddings` is read and never written: the scratch table is a
copy, every index is built on the copy, and no production table gains a column, an index or a
row. Nothing here is `SECURITY DEFINER` and nothing needs to be.

    docker exec -d <api> sh -c 'python -u -m eval index-shape > /tmp/shape.log 2>&1'

Run detached. A dropped client kills the backend mid-`CREATE INDEX` and leaves the schema
behind, which is the one failure mode the `finally` cannot cover.
"""

import asyncio
import json
import math
import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.core.config import settings
from app.core.hardware import active as active_profile
from app.features.embeddings.client import MODEL, VERSION, TeiClient
from app.features.retrieval.search import CANDIDATES
from eval.harness import installation
from eval.questions import load_questions

REPORT = Path(__file__).parent / "index-shape.json"

#: One schema, one name, dropped in a `finally`. Two other agents are measuring against this
#: same database in `zenith_coarse` and `zenith_dims`; nothing here touches either.
SCHEMA = "zenith_ivf"

#: The production HNSW parameters, so the graph measured here is the one the installation
#: runs rather than a differently-tuned cousin. From migration 0025.
M = 16
EF_CONSTRUCTION = 64

#: HNSW's quality knob. The `cpu` profile ships 100; the sweep brackets it so the shape of
#: the curve is visible rather than one point on it.
#:
#: **10 is below the query's `LIMIT` on purpose, and it does not collapse onto it.**
#: `quantisation.json` carries a caveat saying pgvector raises the effective `ef_search` to
#: the `LIMIT`, which would make every value under 50 report the same numbers here. It does
#: not: at pgvector 0.8.0, `ef_search = 10` against `LIMIT 50` returns fifty rows from a walk
#: that was never widened, and its recall@10 is materially worse than `ef_search = 50`'s. The
#: row is kept because a measured refutation of a caveat in a neighbouring report is worth
#: more than one fewer line of output.
EF_SEARCH = (10, 50, 100, 200, 400, 800)

#: IVF's build-time knob, and the one that decides everything else about it. The two usual
#: rules give `rows/1000 = 14` and `sqrt(rows) = 116` for this corpus; the sweep spans a
#: decade either side of both, because the point is the shape and not a recommendation.
LISTS = (10, 30, 100, 300, 1000, 3000)

#: IVF's query-time knob. `probes = lists` is a full scan of the vectors through the index
#: and is always measured as the last point: it is the recall ceiling of the representation,
#: and the reference against which "how much did we have to read" is read.
PROBES = (1, 2, 5, 10, 20, 50, 100)

#: The `lists` value in the sweep closest to `sqrt(rows)` for this corpus — 116 rounded to
#: the decade grid. It is the rule that survives at the projected sizes, where `rows/1000`
#: asks for more lists than pgvector allows, so it is the point the projection reads its
#: probe assumption from.
_SQRT_RULE_LISTS = 100

#: pgvector's hard bound on `lists`, from the reloption. It binds on the projection — the
#: `rows/1000` rule at 322M vectors asks for 322,000 lists and cannot have them.
IVFFLAT_MAX_LISTS = 32768

#: Recall depths. 10 is the headline the brief asks for and the depth a reader's page is
#: drawn from; `CANDIDATES` is what the dense half of the real pipeline asks the index for,
#: so it is the depth the product's behaviour actually depends on.
DEPTHS = (10, CANDIDATES)

#: Hamming neighbours re-ordered by exact fp32 cosine for the `bit` arm. `quantisation.json`
#: swept the width from 20 to 400; one width is carried here because this report is about
#: index shape and the width belongs to that one. 200 is its mid point.
BIT_RESCORE = 200

#: Repeats per query per point; the fastest is kept, so the figure is what the scan costs
#: rather than what the container happened to be doing. Three rather than five because this
#: sweep has ~130 points to `quantisation.py`'s 7.
REPEATS = 3

#: HNSW construction spills and crawls at the default 64 MB, which would report a build time
#: for the memory setting rather than for the index. Recorded in the report beside the numbers
#: it produced.
#:
#: 512 MB was the first value tried and it is not enough: an IVF build at `lists = 3000` over
#: `vector(1024)` asks for 812 MB and refuses outright rather than spilling. That is not a
#: nuisance, it is the finding that `build_wall` reports — pgvector's k-means samples ~50
#: vectors per list and holds the sample in `maintenance_work_mem`, so the memory an IVF build
#: needs is linear in `lists` and in the width of the representation, and it is needed by the
#: *builder*, at once, in a way the finished index never needs again.
MAINTENANCE_WORK_MEM = "1GB"

#: Postgres page size. Read back from the server at runtime and cross-checked, because every
#: footprint number here is a page count.
BLOCK_SIZE = 8192

#: The two storage costs the whole argument turns on, as *assumptions* rather than
#: measurements — nothing on this machine reads from a cold NVMe. A random 8 KB read at
#: ~80 µs is the figure HNSW's residency requirement is justified by; 2 GB/s is a
#: conservative sequential rate for the same device. Both are named here so a reader who
#: disagrees can rescale the projection rather than having to redo it.
RANDOM_READ_US = 80.0
SEQUENTIAL_BYTES_PER_SECOND = 2_000_000_000

#: Corpus sizes to project to, in passages. The middle one is a million documents at this
#: corpus's measured passages-per-document, which is the number the exercise is about.
PROJECTIONS = (10_000_000, 100_000_000, 322_000_000, 1_000_000_000)


@dataclass(frozen=True, slots=True)
class Precision:
    """One stored representation, and the operator that searches it."""

    #: Report key: `fp32`, `fp16`, `bit`.
    key: str
    column: str
    #: The opclass suffix is shared between the two access methods — `hnsw (col X_cosine_ops)`
    #: and `ivfflat (col X_cosine_ops)` — which is exactly what makes the comparison fair:
    #: same vectors, same distance, two index shapes.
    opclass: str
    #: How a query embedding is cast to meet the column.
    cast: str
    operator: str
    #: `bit` is retrieved by Hamming distance and then re-ordered against the fp32 vectors in
    #: the heap. Without that step its recall against exact cosine is a statement about the
    #: quantisation and not about the index.
    rescore: bool = False


PRECISIONS = (
    Precision(
        key="fp32",
        column="embedding",
        opclass="vector_cosine_ops",
        cast="vector",
        operator="<=>",
    ),
    Precision(
        key="fp16",
        column="embedding_half",
        opclass="halfvec_cosine_ops",
        cast="halfvec(1024)",
        operator="<=>",
    ),
    Precision(
        key="bit",
        column="embedding_bit",
        opclass="bit_hamming_ops",
        cast="bit(1024)",
        operator="<~>",
        rescore=True,
    ),
)

TABLE = f"{SCHEMA}.vectors"
EMPTY = f"{SCHEMA}.empty"


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def _query_expression(precision: Precision) -> str:
    """The right-hand side of the order-by, given a query embedding bound as `:q`.

    `bit` is quantised inside the database from the same fp32 embedding the other two use, so
    all three arms answer the same question from the same vector and no arm gets a query the
    others did not see.
    """
    if precision.key == "bit":
        return "binary_quantize(CAST(:q AS vector))::bit(1024)"
    return f"CAST(:q AS {precision.cast})"


def _order_by(precision: Precision) -> str:
    return f"{precision.column} {precision.operator} {_query_expression(precision)}"


async def _create(conn: AsyncConnection, space: Any) -> int:
    """Copy the corpus into the scratch schema in all three representations.

    Ordered by `md5(chunk_id)` so a re-run builds the same table, and computed in one
    `CREATE TABLE AS` rather than by a later `UPDATE`: an update rewrites every heap tuple and
    doubles the entries any already-built index carries, which silently doubled an index size
    during this sweep's development.
    """
    await conn.execute(
        text(
            f"CREATE TABLE {TABLE} AS "
            "SELECT chunk_id, embedding, "
            "embedding::halfvec(1024) AS embedding_half, "
            "binary_quantize(embedding)::bit(1024) AS embedding_bit "
            "FROM chunk_embeddings "
            "WHERE embedding_model = :model AND embedding_version = :version "
            "ORDER BY md5(chunk_id::text)"
        ),
        {"model": space.model, "version": space.version},
    )
    await conn.execute(text(f"ANALYZE {TABLE}"))
    # A structurally identical table holding nothing, for the centroid-footprint probe.
    await conn.execute(text(f"CREATE TABLE {EMPTY} AS SELECT * FROM {TABLE} WHERE false"))
    return int((await conn.execute(text(f"SELECT count(*) FROM {TABLE}"))).scalar_one())


async def _geometry(conn: AsyncConnection, precision: Precision) -> dict[str, Any]:
    """How many bytes of centroid a list costs, measured rather than modelled.

    An ivfflat index over an empty table contains everything that does not depend on the
    vectors: one metapage, the list descriptors, and one blank start page per list. Building
    it at two `lists` values gives a slope,

        d(pages) / d(lists) = 1 + 1 / centroids_per_page

    from which `centroids_per_page` follows without knowing anything about pgvector's structs.
    The same quantity is then read a second way — `pages - lists - 1` centroid pages at each
    point — and the two are cross-checked. `agrees` false means the layout assumption behind
    every resident-footprint number in this report is wrong, and the numbers should not be
    read.
    """
    low, high = min(LISTS), max(LISTS)
    pages: dict[int, int] = {}
    for lists in (low, high):
        name = f"{SCHEMA}.empty_{precision.key}_{lists}"
        await conn.execute(
            text(
                f"CREATE INDEX empty_{precision.key}_{lists} ON {EMPTY} "
                f"USING ivfflat ({precision.column} {precision.opclass}) WITH (lists = {lists})"
            )
        )
        size = (await conn.execute(text("SELECT pg_relation_size(:n)"), {"n": name})).scalar_one()
        await conn.execute(text(f"DROP INDEX {name}"))
        pages[lists] = int(size) // BLOCK_SIZE

    slope = (pages[high] - pages[low]) / (high - low)
    centroids_per_page = 1.0 / (slope - 1.0) if slope > 1.0 else float("inf")
    rounded = max(1, round(centroids_per_page))
    agrees = all(pages[lists] - lists - 1 == math.ceil(lists / rounded) for lists in (low, high))
    return {
        "empty_index_pages": {str(lists): pages[lists] for lists in (low, high)},
        "pages_per_extra_list": round(slope, 6),
        "centroids_per_page": rounded,
        "centroid_bytes_per_list": round(BLOCK_SIZE / rounded, 1),
        # The two derivations agree, so `pages - lists - 1` is the centroid table and the
        # per-list page is a data page rather than part of what must stay resident.
        "agrees": agrees,
        "method": (
            "centroids_per_page from the slope of empty-index pages against lists; "
            "cross-checked against ceil(lists / centroids_per_page) == pages - lists - 1"
        ),
    }


async def _truth(conn: AsyncConnection, vectors: list[list[float]]) -> list[list[UUID]]:
    """Exact fp32 nearest neighbours, with every index refused.

    Run in its own transaction. `SET LOCAL` lasts until the transaction ends, so computing
    ground truth inside the transaction that later runs the approximate queries turns those
    queries into sequential scans as well and reports recall 1.0000 for an index that was
    never consulted. A separate transaction is the fix that cannot be forgotten later; the
    alternative — restoring the setting by hand afterwards — is one edit away from being wrong
    again, which is how it went wrong the first time.
    """
    await conn.execute(text("SET LOCAL enable_indexscan = off"))
    await conn.execute(text("SET LOCAL enable_bitmapscan = off"))
    truth: list[list[UUID]] = []
    for vector in vectors:
        rows = await conn.execute(
            text(
                f"SELECT chunk_id FROM {TABLE} ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
            ),
            {"q": str(vector), "k": max(DEPTHS)},
        )
        truth.append([row.chunk_id for row in rows])
    return truth


async def _probe(
    conn: AsyncConnection, precision: Precision, vector: list[float], limit: int
) -> tuple[list[UUID], float]:
    """One approximate query, at whatever knob the caller has already set."""
    started = time.perf_counter()
    if not precision.rescore:
        rows = await conn.execute(
            text(f"SELECT chunk_id FROM {TABLE} ORDER BY {_order_by(precision)} LIMIT :k"),
            {"q": str(vector), "k": limit},
        )
        return [row.chunk_id for row in rows], (time.perf_counter() - started) * 1000

    rows = await conn.execute(
        text(f"SELECT chunk_id FROM {TABLE} ORDER BY {_order_by(precision)} LIMIT :w"),
        {"q": str(vector), "w": BIT_RESCORE},
    )
    shortlist = [str(row.chunk_id) for row in rows]
    # The rescore reads fp32 vectors from the heap — random I/O against data the
    # quantisation did not shrink. It is inside the timing because it is inside the query
    # path, and it is the half of the binary story that does not get cheaper.
    rows = await conn.execute(
        text(
            f"SELECT chunk_id FROM {TABLE} WHERE chunk_id = ANY(:ids) "
            "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
        ),
        {"ids": shortlist, "q": str(vector), "k": limit},
    )
    return [row.chunk_id for row in rows], (time.perf_counter() - started) * 1000


def _score(got: list[list[UUID]], truth: list[list[UUID]], which: list[int]) -> dict[str, Any]:
    """Recall against exact fp32, at both depths, mean and worst.

    `which` selects a subset of the questions, and it is the only reason this is a function:
    the answerable and unanswerable sets are scored by the same rule over the same run, so a
    difference between them is a property of the index rather than of two scoring codes.
    """
    stats: dict[str, Any] = {"questions": len(which)}
    for depth in DEPTHS:
        overlaps = [
            len(set(got[i][:depth]) & set(truth[i][:depth])) / len(truth[i][:depth]) for i in which
        ]
        stats[f"recall_at_{depth}"] = round(statistics.mean(overlaps), 4) if overlaps else None
        stats[f"worst_at_{depth}"] = round(min(overlaps), 4) if overlaps else None
    return stats


async def _measure(
    conn: AsyncConnection,
    precision: Precision,
    vectors: list[list[float]],
    truth: list[list[UUID]],
    answerable: list[int],
    unanswerable: list[int],
    index: str,
) -> dict[str, Any]:
    """One index, one knob setting: recall, worst question, latency, pages read.

    **The sequential scan is disabled for the duration, and that is not cheating.** At 13,549
    vectors the corpus is small enough that once a setting asks the index for a large enough
    share of it the planner correctly prefers a scan — `hnsw.ef_search = 10` and
    `ivfflat.probes = 5 of 10 lists` both did, on the first run of this sweep, and reported
    recall exactly 1.0000 for an index that was never opened. That is the same effect
    `search.py`'s `dense` documents from the other side: below roughly 15% of the graph the
    planner abandons HNSW, which is why a small corpus cannot see an index's real behaviour.
    This sweep exists to measure the index at sizes where a scan is not an option, so the scan
    is taken away and `used_index` records that the substitution worked.

    What the planner *would* have done is not thrown away — it is one extra `EXPLAIN` at the
    end, reported as `planner_prefers_index`, and a `false` there says this setting is one the
    optimiser would decline on a corpus this small.
    """
    await conn.execute(text("SET LOCAL enable_seqscan = off"))
    got: list[list[UUID]] = []
    times: list[float] = []
    blocks: list[int] = []
    served = 0

    for vector in vectors:
        fastest: float | None = None
        ids: list[UUID] = []
        for _ in range(REPEATS):
            ids, elapsed = await _probe(conn, precision, vector, max(DEPTHS))
            fastest = elapsed if fastest is None else min(fastest, elapsed)
        got.append(ids)
        times.append(fastest or 0.0)
        touched, used = await _explain(conn, precision, vector, max(DEPTHS), index)
        blocks.append(touched)
        served += used

    await conn.execute(text("SET LOCAL enable_seqscan = on"))
    _, preferred = await _explain(conn, precision, vectors[0], max(DEPTHS), index)
    await conn.execute(text("SET LOCAL enable_seqscan = off"))

    return {
        "headline": _score(got, truth, answerable),
        "unanswerable": _score(got, truth, unanswerable),
        "median_ms": round(statistics.median(times), 3),
        "p95_ms": round(_percentile(times, 0.95), 3),
        "median_shared_blocks": round(statistics.median(blocks)),
        "max_shared_blocks": max(blocks),
        "median_bytes_read": round(statistics.median(blocks)) * BLOCK_SIZE,
        # False anywhere means the planner declined the index for at least one question even
        # with the scan disabled, and that point is a scan wearing an index's name.
        "used_index": served == len(vectors),
        # Whether the optimiser, left alone, would have chosen this index at this corpus size.
        "planner_prefers_index": preferred,
    }


async def _plan(conn: AsyncConnection, statement: str, params: dict[str, Any]) -> Any:
    """One executed plan, as JSON. `BUFFERS` accumulates up the tree, so the root is the total."""
    result = await conn.execute(
        text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {statement}"), params
    )
    payload = result.scalar_one()
    return (json.loads(payload) if isinstance(payload, str) else payload)[0]["Plan"]


def _shared(plan: Any) -> int:
    return int(plan.get("Shared Hit Blocks", 0)) + int(plan.get("Shared Read Blocks", 0))


async def _explain(
    conn: AsyncConnection, precision: Precision, vector: list[float], limit: int, index: str
) -> tuple[int, bool]:
    """Pages touched by one query, and whether `index` is the plan that touched them.

    For the rescored `bit` arm this is the sum over *both* statements. Counting only the
    Hamming probe would report the arm with the smallest index as also having the smallest
    working set, and the whole point of the rescore is that it goes back to the heap for fp32
    vectors that no quantisation shrank. The pages it reads there are part of what the query
    costs and they are counted here.
    """
    width = BIT_RESCORE if precision.rescore else limit
    plan = await _plan(
        conn,
        f"SELECT chunk_id FROM {TABLE} ORDER BY {_order_by(precision)} LIMIT :k",
        {"q": str(vector), "k": width},
    )
    touched, used = _shared(plan), index in json.dumps(plan)
    if precision.rescore:
        rows = await conn.execute(
            text(f"SELECT chunk_id FROM {TABLE} ORDER BY {_order_by(precision)} LIMIT :w"),
            {"q": str(vector), "w": BIT_RESCORE},
        )
        touched += _shared(
            await _plan(
                conn,
                f"SELECT chunk_id FROM {TABLE} WHERE chunk_id = ANY(:ids) "
                "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k",
                {
                    "ids": [str(row.chunk_id) for row in rows],
                    "q": str(vector),
                    "k": limit,
                },
            )
        )
    return touched, used


async def _build(conn: AsyncConnection, statement: str, name: str) -> tuple[int, float]:
    started = time.perf_counter()
    await conn.execute(text(statement))
    build_ms = (time.perf_counter() - started) * 1000
    size = (await conn.execute(text("SELECT pg_relation_size(:n)"), {"n": name})).scalar_one()
    return int(size), build_ms


def _build_wall(exc: OperationalError) -> dict[str, Any]:
    """What an IVF build refused, and by how much.

    pgvector's k-means samples ~50 vectors per list and holds the whole sample in
    `maintenance_work_mem`; when it will not fit it raises rather than spilling. The memory an
    IVF build needs is therefore linear in `lists` *and* in the width of the representation,
    it is needed all at once by the builder, and the finished index never needs it again — a
    completely different constraint from HNSW's, whose build is incremental and whose *query*
    is what wants the memory. `required_bytes_per_list` is the number worth carrying forward:
    it is what a projection to 32,768 lists has to be multiplied by.
    """
    message = str(exc.orig) if exc.orig else str(exc)
    found = re.search(r"memory required is (\d+) MB", message)
    required = int(found.group(1)) * 1024 * 1024 if found else None
    return {
        "message": message.strip(),
        "required_bytes": required,
        "maintenance_work_mem": MAINTENANCE_WORK_MEM,
    }


async def _ivfflat(
    owner: AsyncEngine,
    precision: Precision,
    lists: int,
    centroids_per_page: int,
    rows: int,
    vectors: list[list[float]],
    truth: list[list[UUID]],
    answerable: list[int],
    unanswerable: list[int],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One IVF index at one `lists`, swept over `probes`, built and dropped in one transaction.

    Its own transaction rather than the caller's, so a build that pgvector refuses aborts only
    this point: the rollback takes the half-built index with it and the sweep carries on to the
    next `lists` with the refusal recorded.
    """
    name = f"ix_ivf_{precision.key}_{lists}"
    by_probes: dict[str, Any] = {}
    async with owner.begin() as conn:
        await conn.execute(text(f"SET maintenance_work_mem = '{MAINTENANCE_WORK_MEM}'"))
        index_bytes, build_ms = await _build(
            conn,
            f"CREATE INDEX {name} ON {TABLE} USING ivfflat "
            f"({precision.column} {precision.opclass}) WITH (lists = {lists})",
            f"{SCHEMA}.{name}",
        )
        # `probes = lists` is always the last point: a full pass over the vectors through the
        # index, which is the recall ceiling of the representation and the reference every
        # "how much did we have to read" figure is read against.
        for probes in sorted({*[p for p in PROBES if p <= lists], lists}):
            await conn.execute(text(f"SET LOCAL ivfflat.probes = {probes}"))
            point = await _measure(conn, precision, vectors, truth, answerable, unanswerable, name)
            point["fraction_of_lists_probed"] = round(probes / lists, 4)
            by_probes[str(probes)] = point
            head = point["headline"]
            print(
                f"  ivf  {precision.key:<5} lists={lists:<5} probes={probes:<5} "
                f"r@10 {head['recall_at_10']:.4f} worst {head['worst_at_10']:.2f}  "
                f"{point['median_ms']:.2f} ms  {point['median_shared_blocks']} blocks",
                flush=True,
            )
        await conn.execute(text(f"DROP INDEX {SCHEMA}.{name}"))

    centroid_bytes = math.ceil(lists / centroids_per_page) * BLOCK_SIZE
    # One metapage plus the centroid pages. Everything else in the index is vectors, and
    # vectors are what IVF is allowed to leave on disk.
    resident = centroid_bytes + BLOCK_SIZE
    return {
        "index_bytes": index_bytes,
        "bytes_per_vector": round(index_bytes / rows, 1),
        "build_ms": round(build_ms),
        "build_vectors_per_second": round(rows / (build_ms / 1000), 1),
        "centroid_bytes": centroid_bytes,
        "resident_bytes": resident,
        "resident_bytes_per_vector": round(resident / rows, 3),
        "resident_share_of_index": round(resident / index_bytes, 5),
        # At this corpus a large `lists` wastes a partially filled page per list, which is why
        # `bytes_per_vector` climbs with `lists` here and would not at 322M, where a list holds
        # thousands of vectors rather than four.
        "partial_page_waste_bytes": max(0, lists * BLOCK_SIZE - BLOCK_SIZE),
    }, by_probes


def _projection(
    hnsw_bytes_per_vector: float,
    hnsw_blocks_per_query: int,
    packed_bytes_per_vector: float,
    centroids_per_page: int,
    fraction: float | None,
) -> dict[str, object]:
    """What the two shapes cost at sizes nobody can build on this machine.

    Only the parts that are safe to extrapolate are extrapolated. HNSW's per-vector cost
    depends on `m` and the dimension, neither of which moves with N, so its size is linear
    and its residency requirement is structural. IVF's centroid table depends on `lists`, and
    `lists` is chosen by a rule; both usual rules are applied and the one that exceeds
    pgvector's bound is reported as exceeding it rather than quietly clamped into a number
    that looks reasonable.

    **`fraction` is the assumption this rests on and it is the weakest thing here.** It is
    `probes / lists` at the point where IVF reached HNSW's recall on 13,549 vectors, and it is
    carried across as a *fraction* rather than as a probe count on purpose: ten probes of a
    hundred lists and 1,794 probes of 17,944 lists read the same share of the corpus, and
    carrying the absolute ten would claim that IVF at 322M reads 0.06% of the data for the same
    recall, which is arithmetic rather than evidence. Even the fraction is a conjecture this
    corpus cannot test — a denser space may need more of it or, if k-means separates better at
    scale, less. It is here so the consequence is visible, not because it is trustworthy.

    The two "not resident" costs are computed the way each index would actually pay them.
    HNSW's is `hnsw_blocks_per_query` random reads, because a graph walk cannot know its next
    address before it has read the current node; IVF's is one sequential run per probe, at the
    assumed device rate. The gap between those two numbers is the entire argument for IVF, and
    the fraction above is what decides whether it survives.
    """
    out: dict[str, object] = {}
    for n in PROJECTIONS:
        by_rule: dict[str, object] = {}
        for rule, wanted in (("sqrt(rows)", round(math.sqrt(n))), ("rows/1000", n // 1000)):
            lists = min(wanted, IVFFLAT_MAX_LISTS)
            centroid_bytes = math.ceil(lists / centroids_per_page) * BLOCK_SIZE
            per_list_bytes = (n / lists) * packed_bytes_per_vector
            probes = math.ceil(fraction * lists) if fraction else None
            read = per_list_bytes * probes if probes else None
            by_rule[rule] = {
                "lists_wanted": wanted,
                "lists_used": lists,
                # pgvector's reloption stops at 32,768. Above it the `rows/1000` rule is not
                # merely inadvisable, it is unavailable, and the lists get correspondingly
                # larger — which is the same as saying a probe reads more.
                "capped_by_pgvector": wanted > IVFFLAT_MAX_LISTS,
                "resident_centroid_bytes": centroid_bytes,
                "resident_centroid_gib": round(centroid_bytes / 1024**3, 4),
                "vectors_per_list": round(n / lists),
                "bytes_per_list": round(per_list_bytes),
                # The measured fraction, turned back into a probe count at this scale.
                "fraction_of_corpus_read": fraction,
                "probes_implied": probes,
                # What the build would ask for, at pgvector's ~50 samples per list. The
                # measured `build_wall` entries are what this rate is taken from.
                "build_sample_bytes": round(50 * lists * packed_bytes_per_vector),
                "bytes_read_per_query": round(read) if read else None,
                "gib_read_per_query": round(read / 1024**3, 3) if read else None,
                "sequential_read_ms_per_query": (
                    round(read / SEQUENTIAL_BYTES_PER_SECOND * 1000, 1) if read else None
                ),
            }
        out[str(n)] = {
            "hnsw_resident_gib": round(hnsw_bytes_per_vector * n / 1024**3, 1),
            # If the graph is not resident, every hop is a random read. This is the measured
            # page count of a query at `ef_search = 100` priced at the assumed random-read
            # latency, and it is why nobody runs HNSW off disk.
            "hnsw_random_read_ms_per_query_if_not_resident": round(
                hnsw_blocks_per_query * RANDOM_READ_US / 1000, 1
            ),
            "ivfflat": by_rule,
        }
    return out


async def _run() -> int:
    where = await installation()
    if not where.questions:
        print("No question's document is in this corpus — nothing to measure.")
        return 1

    # `installation()` already drops questions with no sources and questions whose document
    # was never uploaded, so it *is* the answerable set. The unanswerable twelve are loaded
    # separately: they have no source document to be present or absent, so no readiness rule
    # applies to them, and they are never mixed into the headline.
    answerable_questions = [question.question for question in where.questions]
    unanswerable_questions = [
        question.question for question in load_questions() if not question.sources
    ]
    questions = answerable_questions + unanswerable_questions
    answerable = list(range(len(answerable_questions)))
    unanswerable = list(range(len(answerable_questions), len(questions)))

    hardware = active_profile()
    owner = create_async_engine(settings.database_owner_url)
    print(
        f"{where.space.n} embeddings, {len(answerable)} answerable + {len(unanswerable)} "
        f"unanswerable questions, profile {hardware.name}"
    )

    embedder = TeiClient(profile=hardware)
    vectors = [await embedder.embed_query(question) for question in questions]
    print(f"{len(vectors)} questions embedded through {MODEL} {VERSION}\n")

    report: dict[str, object] = {}
    hnsw: dict[str, Any] = {}
    ivfflat: dict[str, Any] = {}
    geometry: dict[str, Any] = {}

    try:
        async with owner.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
            await conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
            await conn.execute(text(f"SET maintenance_work_mem = '{MAINTENANCE_WORK_MEM}'"))
            rows = await _create(conn, where.space)
            block_size = int(
                (await conn.execute(text("SELECT current_setting('block_size')::int"))).scalar_one()
            )
            shared_buffers = (
                await conn.execute(text("SELECT current_setting('shared_buffers')"))
            ).scalar_one()
            parallel_workers = (
                await conn.execute(
                    text("SELECT current_setting('max_parallel_maintenance_workers')")
                )
            ).scalar_one()
            # `pg_total_relation_size`, not `pg_relation_size`: a 1024-dimension vector is
            # far past the TOAST threshold, so the heap holds pointers and almost all of the
            # data is in the TOAST relation. The bare heap size is about 3 MB and means
            # nothing here; every "how much of the corpus did the query read" comparison is
            # against the total.
            table_bytes = int(
                (
                    await conn.execute(text("SELECT pg_total_relation_size(:n)"), {"n": TABLE})
                ).scalar_one()
            )
            counted = (
                await conn.execute(text("SELECT count(DISTINCT document_id), count(*) FROM chunks"))
            ).one()
            documents, chunks = int(counted[0]), int(counted[1])
            for precision in PRECISIONS:
                geometry[precision.key] = await _geometry(conn, precision)
                shape: dict[str, Any] = geometry[precision.key]
                print(
                    f"  geometry {precision.key:<5} "
                    f"{shape['centroids_per_page']} centroids/page, "
                    f"{shape['centroid_bytes_per_list']} B/list, agrees={shape['agrees']}"
                )

        if block_size != BLOCK_SIZE:
            print(f"block_size is {block_size}, not {BLOCK_SIZE} — footprints would be wrong.")
            return 1

        # Its own transaction. See `_truth`: `SET LOCAL enable_indexscan = off` outlives the
        # statement and would silently turn every measurement below into a sequential scan.
        async with owner.begin() as conn:
            truth = await _truth(conn, vectors)
        print(f"\n  ground truth: exact fp32 top-{max(DEPTHS)}, sequential scan, no index\n")

        for precision in PRECISIONS:
            centroids_per_page = int(geometry[precision.key]["centroids_per_page"])

            async with owner.begin() as conn:
                await conn.execute(text(f"SET maintenance_work_mem = '{MAINTENANCE_WORK_MEM}'"))
                name = f"ix_hnsw_{precision.key}"
                index_bytes, build_ms = await _build(
                    conn,
                    f"CREATE INDEX {name} ON {TABLE} USING hnsw "
                    f"({precision.column} {precision.opclass}) "
                    f"WITH (m = {M}, ef_construction = {EF_CONSTRUCTION})",
                    f"{SCHEMA}.{name}",
                )
                by_ef: dict[str, Any] = {}
                for ef in EF_SEARCH:
                    await conn.execute(text(f"SET LOCAL hnsw.ef_search = {ef}"))
                    point = await _measure(
                        conn, precision, vectors, truth, answerable, unanswerable, name
                    )
                    by_ef[str(ef)] = point
                    head = point["headline"]
                    print(
                        f"  hnsw {precision.key:<5} ef={ef:<4} "
                        f"r@10 {head['recall_at_10']:.4f} worst {head['worst_at_10']:.2f}  "
                        f"{point['median_ms']:.2f} ms  {point['median_shared_blocks']} blocks",
                        flush=True,
                    )
                await conn.execute(text(f"DROP INDEX {SCHEMA}.{name}"))

            hnsw[precision.key] = {
                "index_bytes": index_bytes,
                "bytes_per_vector": round(index_bytes / rows, 1),
                "build_ms": round(build_ms),
                "build_vectors_per_second": round(rows / (build_ms / 1000), 1),
                # Not an estimate. An HNSW query's next address is only known once the
                # current node has been read, so a partially resident graph pays a random
                # read on every miss; there is no subset of the graph a traversal stays
                # inside. The whole index is the working set.
                "resident_bytes": index_bytes,
                "resident_bytes_per_vector": round(index_bytes / rows, 1),
                "resident_share_of_index": 1.0,
                "by_ef_search": by_ef,
            }
            print()

            by_lists: dict[str, Any] = {}
            for lists in LISTS:
                try:
                    built, by_probes = await _ivfflat(
                        owner,
                        precision,
                        lists,
                        centroids_per_page,
                        rows,
                        vectors,
                        truth,
                        answerable,
                        unanswerable,
                    )
                except OperationalError as exc:
                    # A build that will not fit is a result, not a crash. pgvector refuses
                    # outright rather than spilling, and the message carries the number.
                    by_lists[str(lists)] = {"build_wall": _build_wall(exc)}
                    print(
                        f"  ivf  {precision.key:<5} lists={lists:<5} "
                        f"build refused: {by_lists[str(lists)]['build_wall']['message']}",
                        flush=True,
                    )
                    continue
                by_lists[str(lists)] = {**built, "by_probes": by_probes}
                print()
            ivfflat[precision.key] = {"by_lists": by_lists}

        report = _assemble(
            rows=rows,
            documents=documents,
            chunks=chunks,
            table_bytes=table_bytes,
            hardware=hardware.name,
            shared_buffers=str(shared_buffers),
            parallel_workers=str(parallel_workers),
            answerable=len(answerable),
            unanswerable=len(unanswerable),
            geometry=geometry,
            hnsw=hnsw,
            ivfflat=ivfflat,
        )
        REPORT.write_text(json.dumps(report, indent=2) + "\n")
        print(f"\nWritten to {REPORT.name}")
    finally:
        # Unconditional, and verified rather than asserted. The rule exists because an
        # earlier investigation left two undeclared `SECURITY DEFINER` functions installed
        # and readable by PUBLIC for months.
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


def _crossing(hnsw: dict[str, Any], ivfflat: dict[str, Any]) -> dict[str, Any]:
    """Where IVF reaches HNSW, and what it read to get there.

    The target is HNSW's mean recall@10 at `ef_search = 100`, which is what the `cpu` profile
    ships and therefore what the installation actually delivers today. For each `lists`, the
    smallest `probes` that meets it is reported with the fraction of lists that implies and
    the pages the query actually touched. `null` means no probe count in the sweep reached it,
    which for `probes = lists` — a full scan through the index — means the *representation*
    cannot reach it and no amount of probing will.
    """
    verdict: dict[str, Any] = {}
    for key, arm in hnsw.items():
        baseline = arm["by_ef_search"]["100"]["headline"]["recall_at_10"]
        reference_blocks = arm["by_ef_search"]["100"]["median_shared_blocks"]
        per_lists: dict[str, Any] = {}
        for lists, built in ivfflat[key]["by_lists"].items():
            if "by_probes" not in built:
                # The build was refused for want of memory. Nothing to cross.
                per_lists[lists] = None
                continue
            reached = None
            for probes, point in sorted(built["by_probes"].items(), key=lambda kv: int(kv[0])):
                if point["headline"]["recall_at_10"] >= baseline:
                    reached = {
                        "probes": int(probes),
                        "fraction_of_lists_probed": point["fraction_of_lists_probed"],
                        "recall_at_10": point["headline"]["recall_at_10"],
                        "worst_at_10": point["headline"]["worst_at_10"],
                        "median_shared_blocks": point["median_shared_blocks"],
                        "blocks_against_hnsw": round(
                            point["median_shared_blocks"] / reference_blocks, 2
                        )
                        if reference_blocks
                        else None,
                        "resident_bytes": built["resident_bytes"],
                        "resident_against_hnsw": round(
                            built["resident_bytes"] / arm["resident_bytes"], 5
                        ),
                    }
                    break
            per_lists[lists] = reached
        verdict[key] = {
            "hnsw_recall_at_10_at_ef_100": baseline,
            "hnsw_median_shared_blocks_at_ef_100": reference_blocks,
            "ivfflat_first_probes_reaching_it": per_lists,
        }
    return verdict


def _assemble(**kw: Any) -> dict[str, object]:
    """The report, and the caveats without which its numbers mislead."""
    rows = int(kw["rows"])
    hnsw: dict[str, Any] = kw["hnsw"]
    ivfflat: dict[str, Any] = kw["ivfflat"]
    geometry: dict[str, Any] = kw["geometry"]
    crossing = _crossing(hnsw, ivfflat)

    projections: dict[str, Any] = {}
    for precision in PRECISIONS:
        key = precision.key
        # The packed cost of one vector inside an ivfflat index, taken at the smallest
        # `lists` where the partial-page waste is a rounding error rather than the number.
        smallest = next(
            built
            for lists in LISTS
            if "by_probes" in (built := ivfflat[key]["by_lists"][str(lists)])
        )
        packed = (smallest["index_bytes"] - smallest["resident_bytes"]) / rows
        # The probe count that reached HNSW's recall at the sqrt rule's `lists`, which is the
        # rule that survives at the projected sizes. `None` if it was never reached.
        at_sqrt: dict[str, Any] | None = crossing[key]["ivfflat_first_probes_reaching_it"].get(
            str(_SQRT_RULE_LISTS)
        )
        projections[key] = {
            "packed_bytes_per_vector_in_ivfflat": round(packed, 1),
            "measured_at_lists": _SQRT_RULE_LISTS,
            "why_this_lists": (
                "The fraction is read at lists = 100 because that is the sweep's point "
                "nearest sqrt(13549) = 116, and the projection applies the same sqrt rule — "
                "so a list here holds ~135 vectors and stands in the same relation to its "
                "corpus as the projected ~17,945 do to 322M. The larger-lists rows under "
                "`crossing` reach the same recall on a much smaller fraction, down to 1.7% "
                "at lists = 3000, and that is not a cheaper option hiding in the data: at "
                "lists = 3000 a list holds four vectors, probing fifty of them reads 226 "
                "vectors, and nothing about that is structurally comparable to probing lists "
                "that hold ten thousand each. The fraction is the transferable quantity only "
                "between points where the partitioning is the same shape."
            ),
            "probes_measured": at_sqrt["probes"] if at_sqrt else None,
            "fraction_assumed": at_sqrt["fraction_of_lists_probed"] if at_sqrt else None,
            "sizes": _projection(
                float(hnsw[key]["resident_bytes_per_vector"]),
                int(crossing[key]["hnsw_median_shared_blocks_at_ef_100"]),
                packed,
                int(geometry[key]["centroids_per_page"]),
                at_sqrt["fraction_of_lists_probed"] if at_sqrt else None,
            ),
        }

    return {
        "corpus": {
            "documents": kw["documents"],
            "chunks": kw["chunks"],
            "passages_per_document": round(kw["chunks"] / kw["documents"], 1),
            "vectors_indexed": rows,
            "scratch_table_total_bytes": kw["table_bytes"],
            "scratch_table_note": (
                "Heap plus TOAST plus indexes at the moment it was read, for all three "
                "representations at once. It is the size the sequential-scan alternative "
                "has to move, and the scale against which median_shared_blocks is read."
            ),
        },
        "questions": {
            "answerable": kw["answerable"],
            "unanswerable": kw["unanswerable"],
            "note": (
                "`answerable` counts the questions with a source passage whose document is "
                "loaded here; questions.toml holds 31 answerable and 12 unanswerable, and a "
                "question whose document was never uploaded is dropped rather than scored as "
                "a miss — live.py's rule. The unanswerable twelve have no correct "
                "passage, so their exact top ten is whatever the embedding space happens to "
                "put nearest a question the corpus cannot answer; reproducing that ordering "
                "is the reproduction of noise. They are scored by the same rule and reported "
                "separately under `unanswerable` at every point."
            ),
        },
        "settings": {
            "profile": kw["hardware"],
            "maintenance_work_mem": MAINTENANCE_WORK_MEM,
            "shared_buffers": kw["shared_buffers"],
            "max_parallel_maintenance_workers": kw["parallel_workers"],
            "block_size": BLOCK_SIZE,
            "repeats": REPEATS,
            "depths": list(DEPTHS),
            "hnsw": {
                "m": M,
                "ef_construction": EF_CONSTRUCTION,
                "ef_search_swept": list(EF_SEARCH),
            },
            "ivfflat": {
                "lists_swept": list(LISTS),
                "probes_swept": list(PROBES),
                "max_lists": IVFFLAT_MAX_LISTS,
                "bit_rescore_width": BIT_RESCORE,
            },
            "assumed_storage": {
                "random_8k_read_us": RANDOM_READ_US,
                "sequential_bytes_per_second": SEQUENTIAL_BYTES_PER_SECOND,
                "note": (
                    "Assumptions, not measurements. Nothing here read from a cold device. "
                    "They exist so the projection can be rescaled rather than redone."
                ),
            },
        },
        "geometry": geometry,
        "hnsw": hnsw,
        "ivfflat": ivfflat,
        "crossing": crossing,
        "projection": projections,
        "caveats": [
            "Latency is indicative only. 128 MB of shared_buffers, 7.75 GB for the whole "
            "Docker VM, two other measurement agents competing for CPU, and a corpus small "
            "enough — see corpus.scratch_table_total_bytes — that a sequential scan is a "
            "respectable plan. Recall, index size and resident footprint are the report.",
            "`median_shared_blocks` counts pages the executor touched, hit or read, not "
            "pages it fetched from disk. On this machine almost everything is a hit. It is a "
            "measure of the working set, which is the question, not of I/O.",
            "The scratch table carries no RLS policy and no join to `chunks`, so "
            "`hnsw.iterative_scan` — which exists in the product precisely because RLS "
            "discards HNSW neighbours after the walk — is off here and irrelevant. A "
            "latency from this report is not comparable to `latency.json`. Whether iterative "
            "scan has an IVF equivalent, and what RLS costs an IVF probe, is unmeasured and "
            "is the first thing anyone taking this route must measure.",
            "`lists` is a build-time parameter. Changing it is a full rebuild, and the build "
            "is k-means — the build-time and build_wall columns here are the whole of what is "
            "known about that, and they are measured at 13,549 vectors. pgvector holds a "
            "sample of ~50 vectors per list in maintenance_work_mem and refuses rather than "
            "spilling, so an unbuildable `lists` appears as `build_wall` with the memory it "
            "asked for rather than as a missing row.",
            "Every measured point ran with `enable_seqscan = off`. At this corpus size the "
            "planner correctly prefers a scan for several of these settings, and left alone "
            "it would have reported recall 1.0000 for an index it never opened. "
            "`planner_prefers_index` records what it would have chosen; `used_index` records "
            "that the forced plan was the index. Both are per point.",
            "IVF recall at 13,549 vectors says little about IVF recall at 322M. Cluster "
            "quality depends on how well k-means separates the space, and the space is "
            "denser at scale. Unlike `quantisation.py` this sweep does not fit a trend "
            "across subset sizes, so it cannot extrapolate recall at all: every recall "
            "number here is a measurement at this corpus size and nothing more.",
            "The IVF build is k-means and k-means is seeded randomly, so two runs of this "
            "sweep produce two different partitionings. Across repeated runs the recall at a "
            "given (lists, probes) moved by up to two points and the first probe count "
            "reaching HNSW's recall moved by one step of the probe ladder. Read `crossing` "
            "as the shape of a boundary, not as a threshold: the trend across lists is "
            "stable, an individual cell is not.",
            "The `bit` arm's latency is not the index's. Its rescore looks the shortlist up "
            "by `chunk_id = ANY(...)` on a scratch table that carries no index on chunk_id, "
            "so every rescored query pays a full pass over the copy — which is why every bit "
            "row reads ~10 ms while its block counts are among the lowest here. The block "
            "counts are the comparable figure for that arm; the milliseconds are not. It is "
            "left unindexed so the arm matches `quantisation.py`'s rescore exactly.",
            "The `bit` arm is rescored against fp32 in the heap at a single width. Its index "
            "is tiny and its resident footprint is the smallest of anything measured, but "
            "the rescore reads fp32 vectors that no quantisation shrank. `quantisation.json` "
            "holds the width sweep behind that trade.",
        ],
    }


#: How `python -m eval` finds this sweep. Declared here rather than listed in `__main__.py`,
#: so adding a measurement is adding a file and nothing else.
COMMAND = "index-shape"
USAGE = "index-shape"


def run() -> int:
    return asyncio.run(_run())
