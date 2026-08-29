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

"""Does the coarse dial hold its setting when the corpus is a hundred times larger?

`coarse.json` found a free operating point: `chunks16/mean` opening **32** groups reproduces
the exact dense baseline end to end — headline Recall@8 0.9000, Recall@1 0.6667 — at 15.6x
fewer resident vectors, reading 3.7% of the passages. That result is true and it does not
extrapolate, because `open` is a **count** and not a fraction.

At 13,549 passages there are 866 groups, so 32 groups is the closest 3.7% of the corpus. At a
million documents there are roughly twenty million groups, and 32 of them is the closest
0.00016%. The dial is being asked to be far more selective on a far denser space, and
**recall at a fixed count must fall as the corpus grows.** The 13,549-passage corpus cannot
say how fast, because it is the thing being extrapolated from.

This is the same trap that nearly produced a wrong verdict on binary quantisation: a
favourable number at 13,549 that did not survive the regime the product is aimed at.

## The corpus

`eval/scale.py` already built one, and this reuses it rather than inventing a second: seed
vectors are the real corpus's passages, and synthetic points are **interpolants between near
neighbours**, five positions along the segment between a passage and each of its twenty
nearest. That densifies regions the corpus already occupies, which is what an archive does as
it grows. Its docstring records why the two obvious alternatives are both wrong — random unit
vectors in 1024 dimensions are nearly orthogonal and would flatter any pruning scheme, and
noise-perturbed copies build a tight shell around every original and would punish it.

`scale.py`'s `_build` is called here unchanged, so the vectors are the same vectors.

## The one thing scale.py's ladder cannot be reused for, and what replaces it

`scale.py` subsets by `md5` of the generating triple, which is right for a density ladder and
wrong for this one. **A grouping is destroyed by a random subset**: take 1% of the rows at
random and each group of sixteen keeps one member, so a "group of 16" at the small end of the
ladder is not a group at all and the coarse stage would be measured against a structure that
does not exist at any other rung.

So the ladder here is over the **interpolant variant** instead. Every seed has exactly 100
interpolants — twenty neighbours times five positions — and variant `v` selects one of them
for every passage at once. Level `V` keeps variants `1..V`, which means:

- the corpus is `13,549 x V` passages, nested as `V` grows;
- **groups are real groups.** Synthetic group `(g, v)` holds the `v`-th interpolant of each
  of the sixteen passages of real group `g`. It is a displaced copy of a real `chunks16`
  group, so its internal coherence is the real corpus's coherence by construction rather
  than by hope — and that coherence is the entire mechanism the coarse stage depends on;
- `V = 1` is 13,549 passages in 866 groups: **the real corpus's exact shape**, which is the
  rung the known result has to reappear on;
- group count grows with the corpus, `866 x V`, so the memory factor is held at 15.6x
  throughout and the only thing moving is scale.

## Why there is no fine stage in this file, and why that is exact rather than a shortcut

The fine stage rescans the opened groups exactly and keeps the ten nearest. The truth is the
ten nearest in the **whole** corpus. Any truth passage that lies inside an opened group is
therefore necessarily inside that rescan's top ten — it cannot be displaced by a passage that
is further away. So

    recall@10 at `open` = |{truth passages whose group ranks <= open}| / 10

is not an approximation of what `coarse.py` measured in SQL, it is the same number. Reducing
the measurement to the **group rank of each truth passage** removes the fine stage, removes
the ladder quantisation — the required `open` comes out as a rank rather than as the next rung
up — and makes a hundred-times-larger corpus cheap enough to measure at all.

`gate_real` is what licenses that claim empirically rather than by argument: the same code
path, run on the real corpus with the real `chunks16` grouping, must reproduce
`coarse.json`'s `chunks16/mean` arms rung for rung. If it does not, nothing below it is
readable and the run says so.

## What is reported

Per rung: the dial position needed to hold recall at several targets, the fraction of
passages that costs, and — the ladder-free version of the same thing — the distribution over
questions of the **worst group rank a truth passage lands at**, which is the smallest `open`
that loses that question nothing. Then a log-log fit of required `open` against corpus size,
because the verdict is not "does it still work" but which of three shapes it has: a constant
(`open` stays 32, IO fraction collapses), a fixed fraction (IO fraction pinned at 3.7% and
the absolute read grows with the corpus), or worse than linear.

## What this corpus can and cannot stand in for

`scale.json` records both limits and they are carried here rather than restated favourably.
The synthetic corpus is **denser than a real corpus of the same row count** — 0.1689 against
0.2621 mean distance to the tenth neighbour at 13,549 — so its row count is not its size; the
density is translated through the real corpus's own measured law (`d10 ~ N^-0.107`, refitted
every run) into the real row count that would produce it. And `scale.json`'s calibration gate
came back `mixed` rather than systematically biased, which means the synthetic corpus is not
known to lean either way. That is weaker than a bound in one direction and it is what there
is.

What it cannot stand in for: a corpus with *new subjects* in it. Every synthetic point lies
between two real passages, so a thousand tenants' worth of genuinely unrelated documents
would spread the space in a way this construction never does.

That has one consequence large enough to decide how the result is read. **This corpus can
match a real one in row count or in density, never in both.** At 1,354,900 rows its `d10` is
0.0238, where the real corpus's own fitted law puts a 322-million-passage archive at 0.0624 —
so it holds the right number of group centroids packed roughly two and a half times too
tightly. Centroids that close are harder to tell apart than reality's, which makes the
row-count reading of this ladder the **pessimistic** end. Reading the same rungs by
`equivalent_real_n` instead has the density right and far too few centroids, which is the
**optimistic** end. Both are reported and they differ by orders of magnitude at a million
documents; the run does not pretend to a single number where it has a bracket.

What both readings agree on is the sign, and the sign is the finding: **the required `open` is
not a constant.** A fixed thirty-two is the setting for a corpus of this size and for no
other.

**Read-only.** Everything is built inside `zenith_scale`, dropped in a `finally`;
`chunks` and `chunk_embeddings` are read and never written.

    docker compose exec -T api python -m eval coarse-scale [--levels V,V,...]
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.core.config import settings
from app.core.hardware import active as active_profile
from app.features.embeddings.client import DIMENSION, MODEL, VERSION, TeiClient
from eval.coarse import GROUPINGS
from eval.questions import load_questions
from eval.scale import INTERPOLANTS, NEIGHBOURS, _equivalent, _fit
from eval.scale import SCHEMA as SCALE_SCHEMA
from eval.scale import _build as build_synthetic

REPORT = Path(__file__).parent / "coarse-scale.json"
COARSE = Path(__file__).parent / "coarse.json"

#: The grouping under test. `chunks16/mean` is the only configuration `coarse.json` took to
#: the page and cleared the bar with, so it is the only one whose scaling is a live question.
GROUPING = "chunks16"

#: Recall depth, as in `coarse.json`, `dimensions.json` and `scale.json`.
DEPTH = 10

#: Variants kept, per rung. Every seed has `NEIGHBOURS * INTERPOLANTS` = 100 of them, so the
#: ladder runs from one copy of the real corpus's shape to a hundred, in five steps across
#: two decades. `11` and `100` are the rungs the brief named — 149,039 and 1,354,900
#: passages; `1` is the rung the known result must reappear on.
LEVELS = (1, 3, 11, 33, 100)

#: The dial positions reported per rung. Wider than `coarse.py`'s ladder at the top, because
#: the question is precisely whether the setting has to leave it.
OPEN = (2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768)

#: Mean recall@10 the required `open` is read off at. `0.9355` is not a round number and is
#: not meant to be: it is what `chunks16/mean/open32` scored on the real corpus in
#: `coarse.json`, the operating point that cleared the end-to-end bar. Holding *that* is the
#: question; the round numbers are there so a reader can see the shape rather than one point.
TARGETS = (0.80, 0.90, 0.9355, 0.95, 0.99)

#: Probes for the density statistic, as in `scale.py`, so the two are the same measurement.
DENSITY_PROBES = 60

#: Subsets the real corpus's own density law is fitted over, as in `scale.py`. Taken by seed
#: id, which `scale.py` assigns by `md5(chunk_id)` — a random subset, not the first documents.
DENSITY_FIT_SIZES = (3_000, 6_000, 13_549)

#: Synthetic vectors compared against the ones Postgres generated, to prove the reconstruction
#: used for everything below is the same generator and not a second one that resembles it.
VERIFY_SAMPLE = 500

#: Difference above which the reconstruction is a different generator rather than fp32 noise.
VERIFY_TOLERANCE = 1e-4

#: Corpus sizes, in documents, the fitted shape is projected to. Passages per document is
#: measured at run time.
DOCUMENT_TARGETS = (100_000, 1_000_000)

#: The weights `scale.py`'s `CASE` expresses as repeated addition, read off that expression.
#: `t = 1` is the midpoint; the rest are the quarter, third and two-third positions.
WEIGHTS: tuple[tuple[int, int], ...] = ((1, 1), (2, 1), (1, 2), (3, 1), (1, 3))


def _unit(matrix: Any) -> Any:
    """Rows scaled to unit length. Cosine similarity is then a dot product."""
    import numpy as np

    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    np.maximum(norms, 1e-12, out=norms)
    return (matrix / norms).astype(np.float32, copy=False)


def _top(similarity: Any, depth: int) -> Any:
    """Indices of the `depth` largest entries per row, in order."""
    import numpy as np

    depth = min(depth, similarity.shape[1])
    partial = np.argpartition(-similarity, depth - 1, axis=1)[:, :depth]
    ordering = np.argsort(-np.take_along_axis(similarity, partial, axis=1), axis=1)
    return np.take_along_axis(partial, ordering, axis=1)


def _merge(
    best_similarity: Any, best_index: Any, similarity: Any, offset: int, depth: int
) -> tuple[Any, Any]:
    """Fold one block's top `depth` into a running top `depth`.

    The whole corpus never exists at once; a block is one interpolant variant, 55 MB, and it
    is discarded as soon as it has contributed. This is what makes 1,354,900 vectors an
    arithmetic problem rather than a 5.5 GB table.
    """
    import numpy as np

    top = _top(similarity, depth)
    merged_similarity = np.concatenate(
        [best_similarity, np.take_along_axis(similarity, top, axis=1)], axis=1
    )
    merged_index = np.concatenate([best_index, top.astype(np.int64) + offset], axis=1)
    keep = _top(merged_similarity, depth)
    return (
        np.take_along_axis(merged_similarity, keep, axis=1),
        np.take_along_axis(merged_index, keep, axis=1),
    )


async def _real_corpus(conn: AsyncConnection) -> tuple[Any, Any, list[str]]:
    """Every stored embedding, with the `chunks16` group it belongs to, group-major.

    The grouping expression is imported from `coarse.py` rather than written again. Two
    copies of that SQL would be two groupings, and the whole point of `gate_real` is that
    this file measures the same structure `coarse.json` measured.

    Returned group-major — every group's members contiguous — because that is what makes a
    group mean a `reduceat` over a slice rather than a scatter, both here and for the
    hundred displaced copies of this layout built below.
    """
    import numpy as np

    key = GROUPINGS[GROUPING]
    rows = (
        await conn.execute(
            text(
                f"SELECT c.id::text AS chunk_id, {key} AS gid, e.embedding::text AS vector "
                "FROM chunks c JOIN chunk_embeddings e ON e.chunk_id = c.id "
                "WHERE e.embedding_model = :model AND e.embedding_version = :version"
            ),
            {"model": MODEL, "version": VERSION},
        )
    ).all()

    order = sorted(range(len(rows)), key=lambda i: (rows[i].gid, rows[i].chunk_id))
    vectors = np.empty((len(rows), DIMENSION), dtype=np.float32)
    labels: list[str] = []
    ids: list[str] = []
    for position, source in enumerate(order):
        vectors[position] = np.fromstring(rows[source].vector[1:-1], sep=",", dtype=np.float32)
        labels.append(rows[source].gid)
        ids.append(rows[source].chunk_id)

    starts = [0] + [i for i in range(1, len(labels)) if labels[i] != labels[i - 1]]
    return vectors, np.asarray(starts, dtype=np.int64), ids


def _group_means(vectors: Any, starts: Any) -> Any:
    """One pooled mean per contiguous group. `avg(embedding)`, as the SQL does it."""
    import numpy as np

    sums = np.add.reduceat(vectors, starts, axis=0)
    counts = np.diff(np.append(starts, len(vectors))).astype(np.float32)
    return sums / counts[:, None]


def _ranks_of_truth(queries: Any, group_vectors: Any, truth: Any, member_group: Any) -> Any:
    """For every query, the coarse rank of the group each true neighbour sits in, ascending.

    This is the whole measurement. `open` has to reach the largest of these ten numbers for a
    question to lose nothing, and the k-th smallest for it to keep k of ten.
    """
    import numpy as np

    ordering = np.argsort(-(queries @ group_vectors.T), axis=1)
    rank = np.empty_like(ordering)
    rows = np.arange(len(queries))[:, None]
    rank[rows, ordering] = np.arange(group_vectors.shape[0])[None, :]
    return np.sort(rank[rows, member_group[truth]], axis=1) + 1, ordering


def _sweep(
    truth_ranks: Any, group_sizes: Any, ordering: Any, total: int
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Recall and IO at every dial position, and two ladder-free readings of the same thing.

    The opened groups are different groups for different questions and they are not the same
    size, so the passages read is averaged over questions rather than taken as
    `open x 15.6` — the shortcut would hide exactly the case where a question is expensive
    because the groups near it are the full ones.
    """
    import numpy as np

    groups = len(group_sizes)
    cumulative = np.cumsum(group_sizes[ordering], axis=1)

    arms: list[dict[str, Any]] = []
    for opened in OPEN:
        if opened > groups:
            continue
        hits = (truth_ranks <= opened).sum(axis=1) / truth_ranks.shape[1]
        read = float(cumulative[:, opened - 1].mean())
        arms.append(
            {
                "open": opened,
                "recall_at_10": round(float(hits.mean()), 4),
                "worst_question": round(float(hits.min()), 4),
                "questions_perfect": int((hits >= 1.0).sum()),
                "passages_read": round(read, 1),
                "io_fraction": round(read / total, 6),
                "group_fraction": round(opened / groups, 6),
            }
        )

    full = truth_ranks[:, -1]
    distribution = {
        "note": (
            "Per question, the coarse rank of the worst-placed of its ten true neighbours: "
            "the smallest `open` at which that question loses nothing. Ladder-free, so it "
            "does not round up to the next rung."
        ),
        "median": int(np.median(full)),
        "p75": int(np.percentile(full, 75)),
        "p90": int(np.percentile(full, 90)),
        "max": int(full.max()),
        "median_rank_for_9_of_10": int(np.median(truth_ranks[:, -2])),
        "median_rank_for_8_of_10": int(np.median(truth_ranks[:, -3])),
    }

    # The exact dial position, not the next power of two above it. Mean recall at `open` is
    # the fraction of all pooled truth ranks at or below it, so the smallest `open` reaching a
    # target is one order statistic of that pool. The ladder rounds up by as much as a factor
    # of two, which is most of a decade over five rungs and would go straight into the fitted
    # exponent — the one number this file exists to produce.
    pooled = np.sort(truth_ranks.reshape(-1))
    exact: dict[str, Any] = {}
    for target in TARGETS:
        need = math.ceil(target * len(pooled) - 1e-9)
        if need > len(pooled):
            exact[f"{target:.4f}"] = None
            continue
        opened = int(pooled[need - 1])
        read = float(cumulative[:, opened - 1].mean())
        exact[f"{target:.4f}"] = {
            "open": opened,
            "recall_at_10": round(float((truth_ranks <= opened).mean()), 4),
            "io_fraction": round(read / total, 6),
            "passages_read": round(read, 1),
            "group_fraction": round(opened / groups, 6),
        }
    return arms, distribution, exact


def _required(arms: list[dict[str, Any]]) -> dict[str, Any]:
    """The smallest rung holding each recall target, with what it costs. `None` if none does."""
    out: dict[str, Any] = {}
    for target in TARGETS:
        hit = next((a for a in arms if a["recall_at_10"] >= target), None)
        out[f"{target:.4f}"] = (
            {
                "open": hit["open"],
                "recall_at_10": hit["recall_at_10"],
                "io_fraction": hit["io_fraction"],
                "passages_read": hit["passages_read"],
            }
            if hit
            else None
        )
    return out


def _probe_indices(count: int, total: int) -> Any:
    """A deterministic, evenly spread sample. Two runs probe the same points."""
    import numpy as np

    return np.linspace(0, total - 1, num=min(count, total), dtype=np.int64)


def _density_of(vectors: Any, probes: Any) -> float:
    """Mean cosine distance to the tenth nearest neighbour. `scale.py`'s statistic."""
    import numpy as np

    similarity = vectors[probes] @ vectors.T
    similarity[np.arange(len(probes)), probes] = -2.0
    kept = np.sort(np.partition(similarity, -DEPTH, axis=1)[:, -DEPTH:], axis=1)
    return float((1.0 - kept[:, 0]).mean())


class Synthetic:
    """The interpolated corpus, addressed by index rather than materialised.

    Passage `i` is `l2_normalize(wa * seed[a] + wb * seed[b])` where `a` is the real passage
    at position `i % rows`, `v = i // rows + 1` is the variant, `b` is `a`'s
    `((v - 1) // 5 + 1)`-th nearest neighbour, and the weights come from `(v - 1) % 5`.

    Held as a rule rather than as 5.5 GB of table because every consumer below wants either a
    pooled mean over a contiguous slice or the vectors of one variant at a time, and both are
    a matmul on 55 MB. `_verify` is what makes the rule trustworthy: it is checked against
    the rows Postgres generated from `scale.py`'s own SQL.
    """

    def __init__(self, seeds: Any, neighbours: Any, starts: Any) -> None:
        self.seeds = seeds
        self.neighbours = neighbours
        self.starts = starts
        self.rows = len(seeds)
        self.groups = len(starts)

    def variant(self, v: int) -> Any:
        """Every passage's `v`-th interpolant, in the real corpus's group-major order."""
        rank, position = divmod(v - 1, INTERPOLANTS)
        wa, wb = WEIGHTS[position]
        return _unit(wa * self.seeds + wb * self.seeds[self.neighbours[:, rank]])

    def at(self, indices: Any) -> Any:
        """Arbitrary passages by global index. Used for density probes."""
        import numpy as np

        variants, positions = np.divmod(indices, self.rows)
        ranks, offsets = np.divmod(variants, INTERPOLANTS)
        weights = np.asarray(WEIGHTS, dtype=np.float32)[offsets]
        partners = self.neighbours[positions, ranks]
        return _unit(
            weights[:, 0:1] * self.seeds[positions] + weights[:, 1:2] * self.seeds[partners]
        )

    def member_group(self, levels: int) -> Any:
        """Group index per passage across `levels` variants. Contiguous by construction."""
        import numpy as np

        one = np.repeat(np.arange(self.groups, dtype=np.int32), self.sizes)
        return np.concatenate([one + level * self.groups for level in range(levels)])

    def group_sizes(self, levels: int) -> Any:
        import numpy as np

        return np.tile(self.sizes.astype(np.float64), levels)

    @property
    def sizes(self) -> Any:
        import numpy as np

        return np.diff(np.append(self.starts, self.rows)).astype(np.int32)


async def _seed_order(conn: AsyncConnection, ids: list[str]) -> Any:
    """`zenith_scale.seed.id` for each real passage, in this file's group-major order.

    `scale.py` numbers its seeds `row_number() OVER (ORDER BY md5(chunk_id))` and then drops
    `chunk_id`, so the correspondence has to be recomputed rather than read. The same
    expression over the same rows gives the same numbering, and `_verify` is what proves it
    did — a wrong numbering would put every reconstructed vector somewhere else entirely.
    """
    import numpy as np

    rows = (
        await conn.execute(
            text(
                "SELECT row_number() OVER (ORDER BY md5(chunk_id::text))::int AS sid, "
                "chunk_id::text AS chunk_id FROM chunk_embeddings "
                "WHERE embedding_model = :model AND embedding_version = :version"
            ),
            {"model": MODEL, "version": VERSION},
        )
    ).all()
    by_chunk = {row.chunk_id: int(row.sid) for row in rows}
    return np.asarray([by_chunk[chunk] for chunk in ids], dtype=np.int32)


async def _neighbours(conn: AsyncConnection, seed_id: Any, seeds: Any) -> Any:
    """Each passage's twenty neighbours, in distance order, as positions in this file's order.

    `zenith_scale.pairs` records *which* twenty, not in what order, so the order is recomputed
    from the vectors. It is the ordering `scale.py`'s `LIMIT 20` produced, and where its
    approximate search picked a slightly different twentieth neighbour that is a slightly
    different synthetic point rather than a wrong one — `scale.py` makes the same argument
    for the same reason.
    """
    import numpy as np

    position_of = np.zeros(int(seed_id.max()) + 1, dtype=np.int32)
    position_of[seed_id] = np.arange(len(seed_id), dtype=np.int32)

    partners: dict[int, list[int]] = {}
    for row in await conn.execute(text(f"SELECT a, b FROM {SCALE_SCHEMA}.pairs")):
        partners.setdefault(int(row.a), []).append(int(row.b))
    short = [seed for seed, found in partners.items() if len(found) != NEIGHBOURS]
    if short or len(partners) != len(seed_id):
        raise RuntimeError(
            f"pairs is not {NEIGHBOURS} per seed: {len(partners)} seeds, "
            f"{len(short)} with the wrong count"
        )

    ordered = np.empty((len(seed_id), NEIGHBOURS), dtype=np.int32)
    for index in range(len(seed_id)):
        candidates = position_of[np.asarray(partners[int(seed_id[index])], dtype=np.int32)]
        ordered[index] = candidates[np.argsort(-(seeds[candidates] @ seeds[index]))]
    return ordered


async def _verify(conn: AsyncConnection, corpus: Synthetic, seed_id: Any) -> dict[str, Any]:
    """The reconstruction against the rows `scale.py`'s SQL actually wrote.

    A sample of generating triples is located in `zenith_scale.vecs` through the same `md5`
    ordering that built it, and the vector Postgres stored is compared with the vector this
    file's rule produces. If the two ever disagree, every number in this report is about a
    corpus that was never generated.
    """
    import numpy as np

    await conn.execute(text("SET LOCAL work_mem = '256MB'"))
    await conn.execute(text(f"DROP TABLE IF EXISTS {SCALE_SCHEMA}.origin"))
    await conn.execute(
        text(
            f"CREATE TABLE {SCALE_SCHEMA}.origin AS SELECT row_number() OVER ("
            "ORDER BY md5(p.a::text||':'||p.b::text||':'||w.t::text)) AS id, "
            f"p.a, p.b, w.t FROM {SCALE_SCHEMA}.pairs p "
            "CROSS JOIN (VALUES (1),(2),(3),(4),(5)) w(t)"
        )
    )
    rows = (
        await conn.execute(
            text(
                # Sampled before the join, not after: `ORDER BY ... LIMIT` over the joined
                # relation sorts 1.35M rows carrying a 4 KB vector each, which is 5.5 GB of
                # temporary file. Over `origin` alone it is a top-N sort of small tuples.
                #
                # `AS position`: SQLAlchemy's Row exposes `.t` as its tuple accessor, so a
                # column named `t` comes back as the whole row and the cast below fails.
                f"WITH picked AS (SELECT id, a, b, t FROM {SCALE_SCHEMA}.origin "
                f"  ORDER BY md5(id::text) LIMIT {VERIFY_SAMPLE}) "
                "SELECT o.a AS seed, o.b AS partner, o.t AS position, "
                "v.embedding::text AS vector "
                f"FROM picked o JOIN {SCALE_SCHEMA}.vecs v ON v.id = o.id"
            )
        )
    ).all()

    position_of = np.zeros(int(seed_id.max()) + 1, dtype=np.int32)
    position_of[seed_id] = np.arange(len(seed_id), dtype=np.int32)

    worst = 0.0
    for row in rows:
        home = int(position_of[int(row.seed)])
        partner = int(position_of[int(row.partner)])
        wa, wb = WEIGHTS[int(row.position) - 1]
        mine = wa * corpus.seeds[home] + wb * corpus.seeds[partner]
        mine = mine / np.linalg.norm(mine)
        theirs = np.fromstring(row.vector[1:-1], sep=",", dtype=np.float32)
        worst = max(worst, float(np.abs(mine - theirs).max()))

    return {
        "sampled": len(rows),
        "max_abs_difference": float(f"{worst:.3g}"),
        "tolerance": VERIFY_TOLERANCE,
        "matches": worst <= VERIFY_TOLERANCE,
    }


def _gate(arms: list[dict[str, Any]]) -> dict[str, Any]:
    """The real-corpus rung against what `coarse.json` measured in SQL.

    Not a formality. This file replaces a two-stage SQL retrieval with an argsort over group
    ranks, on the argument that a true neighbour inside an opened group cannot be displaced
    from that group's exact rescan. The argument is sound and it is also exactly the kind of
    thing that is wrong in a way nobody notices, so it is checked against the numbers the SQL
    produced rather than asserted.
    """
    if not COARSE.exists():
        return {"available": False, "reason": "coarse.json not present"}
    published = {
        arm["opened"]: arm
        for arm in json.loads(COARSE.read_text())["arms"]
        if arm["grouping"] == GROUPING and arm["pooling"] == "mean"
    }
    compared: list[dict[str, Any]] = []
    worst = 0.0
    for arm in arms:
        reference = published.get(arm["open"])
        if reference is None:
            continue
        delta = arm["recall_at_10"] - reference["recall"]
        worst = max(worst, abs(delta))
        compared.append(
            {
                "open": arm["open"],
                "coarse_json_recall": reference["recall"],
                "this_run_recall": arm["recall_at_10"],
                "delta": round(delta, 4),
                "coarse_json_worst": reference["worst"],
                "this_run_worst": arm["worst_question"],
                "coarse_json_io": reference["io_fraction"],
                "this_run_io": round(arm["io_fraction"], 4),
            }
        )
    return {
        "available": True,
        "source": "coarse.json chunks16/mean",
        "compared": compared,
        "worst_delta": round(worst, 4),
        "tolerance": 0.0001,
        "reproduces": worst <= 0.0001,
    }


def _shape(rungs: list[dict[str, Any]], passages_per_document: float, ram: float) -> dict[str, Any]:
    """Log-log fit of required `open` against corpus size, per recall target.

    The verdict lives here. An exponent near 0 means a constant dial and an IO fraction that
    collapses as the corpus grows: the fine stage keeps reading a few hundred passages
    whatever the archive holds. Near 1 means a fixed fraction: the IO fraction is pinned
    wherever it is now and the *absolute* read grows with the corpus, which is the page-cache
    trap — 3.7% of 322 million passages is twelve million passages a query. Above 1 there is
    no operating point at all.

    Projected with the fit rather than with the last rung, and both are in the report, because
    a projection three decades past the data is an ordering and not a quantity — the same
    caution `scale.py` attaches to `equivalent_real_n`.
    """
    out: dict[str, Any] = {
        "axis": (
            "Row count. For binary quantisation `scale.py` argued the meaningful axis is "
            "density and the row count is only a proxy, because sign collisions are a local "
            "property. The mechanism here is different and the argument does not carry over: "
            "what a coarse stage competes against is the *number of group centroids*, which "
            "is the row count divided by the group size in the synthetic corpus exactly as it "
            "is in the real one. `by_density` below reads the same rungs the other way, and "
            "the two disagree — see the caveats."
        )
    }
    for target in TARGETS:
        key = f"{target:.4f}"
        points = [
            (float(rung["passages"]), float(rung["required_open_exact"][key]["open"]))
            for rung in rungs
            if rung["required_open_exact"].get(key)
        ]
        if len(points) < 3:
            out[key] = {"exponent": None, "reason": "target reached at fewer than three sizes"}
            continue
        exponent, intercept = _fit(points)
        projected: dict[str, Any] = {}
        for documents in DOCUMENT_TARGETS:
            passages = documents * passages_per_document
            opened = 10 ** (intercept + exponent * math.log10(passages))
            projected[f"{documents:,} documents"] = {
                "passages": round(passages),
                "open": round(opened),
                "passages_read": round(opened * ram),
                "io_fraction": round(opened * ram / passages, 6),
            }
        by_density = [
            (float(rung["equivalent_real_n"]), float(rung["required_open_exact"][key]["open"]))
            for rung in rungs
            if rung["required_open_exact"].get(key) and rung["equivalent_real_n"] > 0
        ]
        density_exponent, density_intercept = _fit(by_density)
        out[key] = {
            "points": [{"passages": int(n), "open": int(o)} for n, o in points],
            "exponent": round(exponent, 3),
            "intercept_log10": round(intercept, 3),
            "by_density": {
                "note": (
                    "The same required `open`, fitted against `equivalent_real_n` instead of "
                    "the row count. `equivalent_real_n` is `scale.py`'s translation of a "
                    "measured density into the real corpus size that would produce it, and "
                    "beyond the decade the law was fitted over it is an ordering rather than "
                    "a quantity — so this fit is a sanity check on the direction, not a "
                    "second answer."
                ),
                "points": [{"equivalent_real_n": int(n), "open": int(o)} for n, o in by_density],
                "exponent": round(density_exponent, 3),
                "at_1m_documents": round(
                    10
                    ** (
                        density_intercept
                        + density_exponent * math.log10(1_000_000 * passages_per_document)
                    )
                ),
            },
            "reading": (
                "constant dial"
                if exponent < 0.2
                else "sublinear"
                if exponent < 0.85
                else "fixed fraction"
                if exponent <= 1.15
                else "worse than linear"
            ),
            "projected": projected,
        }
    return out


async def _hygiene(conn: AsyncConnection) -> dict[str, Any]:
    """This run's own cleanup, verified rather than assumed.

    Only `zenith_scale` and `zenith_coarse` are this run's to drop. Another sweep holds
    `zenith_ivf` in the same database; it is reported so a reader can see it was seen and not
    touched, and it is not counted against `clean`.
    """
    mine = (SCALE_SCHEMA, "zenith_coarse")
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


async def _run(levels: tuple[int, ...]) -> int:
    try:
        import numpy as np
    except ModuleNotFoundError:
        print(
            "numpy is required and is not installed in this container.\n"
            "  uv pip install --python /app/.venv/bin/python 'numpy>=2.1'"
        )
        return 1

    started = time.perf_counter()
    loaded = load_questions()
    # Embedded as one batch of all forty-three and then narrowed, which is what `coarse.py`
    # does. Embedding only the thirty-one would be a different batch through the same model,
    # and `gate_real` compares two top-ten lists rather than two averages — one query vector
    # differing in its last bits is enough to move a borderline neighbour and make a gate
    # that should be exact look approximate.
    embedded = await TeiClient(profile=active_profile()).embed([q.question for q in loaded])
    answerable = [i for i, q in enumerate(loaded) if q.sources]
    queries = _unit(np.asarray([embedded[i] for i in answerable], dtype=np.float32))
    print(f"{len(answerable)} answerable of {len(loaded)} questions, {MODEL} {VERSION}", flush=True)

    engine = create_async_engine(settings.database_owner_url)
    rungs: list[dict[str, Any]] = []

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            vectors, starts, ids = await _real_corpus(conn)
            documents = int(
                (
                    await conn.execute(text("SELECT count(DISTINCT document_id) FROM chunks"))
                ).scalar_one()
            )
            await conn.rollback()

        rows, groups = len(vectors), len(starts)
        per_document = round(rows / documents, 1)
        print(f"real corpus: {rows} passages, {groups} {GROUPING} groups, {documents} documents")

        # ---- the real corpus, through this file's own machinery -------------------------
        real_units = _unit(vectors)
        real_means = _unit(_group_means(vectors, starts))
        real_sizes = np.diff(np.append(starts, rows)).astype(np.float64)
        real_member_group = np.repeat(np.arange(groups, dtype=np.int32), real_sizes.astype(int))
        real_truth = _top(queries @ real_units.T, DEPTH)
        real_ranks, real_order = _ranks_of_truth(queries, real_means, real_truth, real_member_group)
        real_arms, real_distribution, real_exact = _sweep(real_ranks, real_sizes, real_order, rows)
        gate = _gate(real_arms)
        print(
            f"\ngate against coarse.json: "
            f"{'REPRODUCES' if gate.get('reproduces') else 'FAILED'} "
            f"(worst delta {gate.get('worst_delta')})"
        )
        for line in gate.get("compared", []):
            print(
                f"  open {line['open']:>5}  coarse.json {line['coarse_json_recall']:.4f}"
                f"  here {line['this_run_recall']:.4f}  delta {line['delta']:+.4f}"
            )

        # ---- the synthetic corpus, from scale.py's own generator -------------------------
        async with engine.begin() as conn:
            build_started = time.perf_counter()
            built = await build_synthetic(conn)
            print(f"\nbuilt {built} in {time.perf_counter() - build_started:.0f} s", flush=True)
            seed_id = await _seed_order(conn, ids)
            corpus = Synthetic(real_units, await _neighbours(conn, seed_id, real_units), starts)
            verification = await _verify(conn, corpus, seed_id)
            print(f"reconstruction vs scale.py's own rows: {verification}", flush=True)
        if not verification["matches"]:
            raise RuntimeError("reconstruction does not match the generated corpus")

        # The real corpus's density law, refitted here for the reason `scale.py` refits it:
        # a hard-coded exponent would quietly mistranslate every density in the report.
        # Subset by seed id, which is `md5(chunk_id)` order — a random subset of the corpus
        # rather than its first documents.
        law_points: list[tuple[float, float]] = []
        for size in DENSITY_FIT_SIZES:
            kept = real_units[seed_id <= size]
            probes_here = _probe_indices(DENSITY_PROBES, len(kept))
            law_points.append((float(size), _density_of(kept, probes_here)))
        exponent, intercept = _fit(law_points)
        real_density = law_points[-1][1]
        print(
            f"\nreal corpus density law: d10 ~ N^{exponent:.4f} "
            f"(d~{-1 / exponent:.1f}), d10 at {rows} = {real_density:.4f}\n",
            flush=True,
        )

        for level in levels:
            level_started = time.perf_counter()
            member_group = corpus.member_group(level)
            group_sizes = corpus.group_sizes(level)
            total = rows * level
            probes_at = _probe_indices(DENSITY_PROBES, total)
            probes = corpus.at(probes_at)

            means = np.empty((groups * level, DIMENSION), dtype=np.float32)
            truth_similarity = np.full((len(queries), DEPTH), -2.0, dtype=np.float32)
            truth_index = np.zeros((len(queries), DEPTH), dtype=np.int64)
            # `DEPTH + 1` because a probe is a member of the corpus and matches itself; the
            # self-match is struck out below and the tenth of what remains is the statistic.
            probe_similarity = np.full((len(probes), DEPTH + 1), -2.0, dtype=np.float32)
            probe_index = np.zeros((len(probes), DEPTH + 1), dtype=np.int64)

            for v in range(1, level + 1):
                block = corpus.variant(v)
                offset = (v - 1) * rows
                means[(v - 1) * groups : v * groups] = _group_means(block, starts)
                truth_similarity, truth_index = _merge(
                    truth_similarity, truth_index, queries @ block.T, offset, DEPTH
                )
                probe_similarity, probe_index = _merge(
                    probe_similarity, probe_index, probes @ block.T, offset, DEPTH + 1
                )
                del block

            self_match = probe_index == probes_at[:, None]
            probe_similarity = np.where(self_match, -2.0, probe_similarity)
            density = float((1.0 - np.sort(probe_similarity, axis=1)[:, -DEPTH]).mean())

            means = _unit(means)
            truth_ranks, ordering = _ranks_of_truth(queries, means, truth_index, member_group)
            arms, distribution, exact = _sweep(truth_ranks, group_sizes, ordering, total)
            rungs.append(
                {
                    "variants": level,
                    "passages": total,
                    "groups": groups * level,
                    "ram_factor": round(total / (groups * level), 1),
                    "d10": round(density, 4),
                    "equivalent_real_n": round(_equivalent(density, exponent, intercept)),
                    "truth_groups_mean": round(
                        float(
                            np.mean([len(set(row.tolist())) for row in member_group[truth_index]])
                        ),
                        2,
                    ),
                    "required_open": _required(arms),
                    "required_open_exact": exact,
                    "worst_group_rank": distribution,
                    "arms": arms,
                    "took_s": round(time.perf_counter() - level_started, 1),
                }
            )
            entry = rungs[-1]["required_open_exact"]["0.9355"]
            print(
                f"  V={level:>3}  {total:>9,} passages  {groups * level:>7,} groups"
                f"  d10 {density:.4f}  = real {rungs[-1]['equivalent_real_n']:>16,}  "
                + (
                    f"open {entry['open']:>6}  reads {entry['io_fraction']:.4%}"
                    if entry
                    else "target never reached"
                ),
                flush=True,
            )
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCALE_SCHEMA} CASCADE"))
        async with engine.connect() as conn:
            hygiene = await _hygiene(conn)
            await conn.rollback()
        await engine.dispose()

    ram = rows / groups
    shape = _shape(rungs, per_document, ram)
    REPORT.write_text(
        json.dumps(
            {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "took_s": round(time.perf_counter() - started, 1),
                "question": (
                    "`open` is a count, not a fraction. How must it grow with the corpus to "
                    "hold the recall the free operating point in coarse.json reached, and "
                    "what does that do to the fraction of passages the fine stage reads?"
                ),
                "depth": DEPTH,
                "grouping": f"{GROUPING}/mean",
                "questions_scored": len(answerable),
                "real_corpus": {
                    "passages": rows,
                    "groups": groups,
                    "documents": documents,
                    "passages_per_document": per_document,
                    "ram_factor": round(ram, 1),
                    "d10": round(real_density, 4),
                    "density_law": {
                        "points": [{"n": int(n), "d10": round(d, 4)} for n, d in law_points],
                        "exponent": round(exponent, 4),
                        "intercept_log10": round(intercept, 4),
                        "intrinsic_dimension": round(-1 / exponent, 1),
                        "fitted_over": [DENSITY_FIT_SIZES[0], DENSITY_FIT_SIZES[-1]],
                    },
                    "arms": real_arms,
                    "required_open": _required(real_arms),
                    "required_open_exact": real_exact,
                    "worst_group_rank": real_distribution,
                },
                "gate_real": gate,
                "synthetic": {
                    "generator": "eval/scale.py `_build`, called unchanged",
                    "corpus": built,
                    "ladder": (
                        "By interpolant variant, not by md5 subset. A random subset keeps one "
                        "member of each group of sixteen and destroys the structure under "
                        "test; a variant selects one interpolant of every passage at once, so "
                        "level V is 13,549 x V passages in 866 x V groups and every group is "
                        "a displaced copy of a real chunks16 group."
                    ),
                    "verification": verification,
                    "rungs": rungs,
                },
                "shape": shape,
                "caveats": [
                    "The synthetic corpus is denser than a real corpus of the same row count "
                    "(scale.json: 0.1689 against 0.2621 at 13,549), which is the point — row "
                    "count is discarded and the measured density is translated through the "
                    "real corpus's own law into `equivalent_real_n`. That law is fitted over "
                    "3,000 to 13,549 real vectors, so an equivalent far outside that decade "
                    "is an ordering and not a quantity.",
                    "scale.json's calibration gate came back `mixed` rather than "
                    "systematically biased. The synthetic corpus is therefore not known to "
                    "lean either way on this measurement, which is weaker than a one-sided "
                    "bound and is what there is.",
                    "Every synthetic point lies between two real passages, so this corpus "
                    "grows denser without ever growing *broader*. A real archive a thousand "
                    "times the size holds subjects this one does not, and those would spread "
                    "the group centroids in a way this construction cannot show.",
                    "**This corpus can match a real one in row count or in density, never in "
                    "both, and the two readings bracket the answer rather than agreeing on "
                    "it.** At 1,354,900 rows its d10 is 0.0238 against the 0.0624 the fitted "
                    "law gives a real corpus of 322 million passages, so it holds the right "
                    "number of group centroids packed far too tightly: discrimination is "
                    "harder than reality and the row-count reading is therefore the "
                    "pessimistic end. Reading the same rungs by `equivalent_real_n` has the "
                    "density right and far too few centroids, which is the optimistic end. "
                    "They differ by orders of magnitude at a million documents. What both "
                    "agree on, and what this run is entitled to conclude, is the sign: the "
                    "required `open` is not a constant, so a fixed 32 is not the setting at "
                    "any size but this one.",
                    "Index recall, not the page. coarse.json refuted the reasoning that "
                    "dense-stage loss is absorbed downstream — chunks16/mean/open16 has "
                    "better dense recall than binary_r20 and worse headline recall, because "
                    "group pruning discards coherent regions and the lexical half tends to "
                    "fail on the same questions. The end-to-end consequence of a dial "
                    "position is measured on the real corpus in coarse.json and "
                    "coarse-dims.json, and is not inferred from anything here.",
                    "31 questions at depth 10, so the finest difference this can resolve is "
                    "one neighbour of one question: 0.0032.",
                    "No latency. This is numpy in a 7.75 GB VM; the product's cost is a "
                    "Postgres scan over a table that does not fit in shared_buffers, and the "
                    "two have nothing to say to each other. What transfers is the passage "
                    "count, and that is reported.",
                ],
                "hygiene": hygiene,
            },
            indent=2,
        )
        + "\n"
    )

    print("\nrequired `open` to hold recall@10 >= 0.9355 (the free point's own level):")
    for rung in rungs:
        entry = rung["required_open_exact"]["0.9355"]
        print(
            f"  {rung['passages']:>9,} passages  {rung['groups']:>7,} groups  "
            + (
                f"open {entry['open']:>6}   reads {entry['io_fraction']:.4%} "
                f"({entry['passages_read']:,.0f} passages)"
                if entry
                else "never reached on this ladder"
            )
        )
    for target, fit in shape.items():
        # `axis` is a string, not a fit. Everything else in this dict is keyed by target.
        if isinstance(fit, dict) and fit.get("exponent") is not None:
            print(
                f"  target {target}: open ~ N^{fit['exponent']} ({fit['reading']}) "
                f"-> {fit['projected']['1,000,000 documents']['passages_read']:,} passages a "
                f"query at 1M documents; by density instead, "
                f"open ~ Neq^{fit['by_density']['exponent']} -> "
                f"{fit['by_density']['at_1m_documents']:,}"
            )
    print(f"\nhygiene: {hygiene}")
    print(f"Written to {REPORT.name}")
    return 0 if hygiene["clean"] else 1


COMMAND = "coarse-scale"
USAGE = "coarse-scale [--levels V,V,...]"


def run(levels: tuple[int, ...] = LEVELS) -> int:
    return asyncio.run(_run(levels))


def cli(argv: list[str]) -> int:
    """`--levels V,V,...` in interpolant variants (1..100), or the default ladder."""
    if "--levels" in argv:
        return run(tuple(int(n) for n in argv[argv.index("--levels") + 1].split(",")))
    return run()
