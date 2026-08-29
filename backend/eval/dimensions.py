# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""How many dimensions this corpus actually needs, and what fewer of them cost in recall.

The vector index is the structural ceiling on how much corpus one installation can hold.
`eval/quantisation.py` measured the first lever on that ceiling — precision — and migration
0025 took the 3x it offered by moving the index to `halfvec`. This sweep measures the third
lever, which is orthogonal to it and multiplies with it: **the number of components each
passage is stored with.**

`halfvec(1024)` at `m=16, ef_construction=64` is a measured ~2,730 bytes of HNSW per passage.
Roughly two thousand of those bytes are the vector itself, so the width of the vector is very
nearly the whole cost, and 1024 -> 256 is close to 4x. That is a large enough number that it
has to be measured rather than argued.

## Why truncation is not the experiment

BGE-M3 is not published as a Matryoshka model. Nothing in its training asks the first 256
components to be a usable embedding on their own, so slicing the vector has no guarantee
behind it and any result would be luck. A **basis fitted on this corpus's own embeddings** is
a different claim: it finds the directions this corpus varies in, which is a property of the
documents rather than of the model, and it is exactly the kind of claim that can only be
settled by fitting it and looking.

## Three families, because "PCA" turned out to be two decisions

Textbook PCA subtracts the corpus mean and then rotates onto the leading eigenvectors of the
covariance. Only the second half of that is a dimension reduction. **Centring is not a
rotation**: it moves every point, changes every norm, and cosine is a function of the norms —
so a centred index ranks by a different metric and returns different neighbours even at full
width, before a single component has been dropped.

That is not a detail, it is the largest single effect in this report, and a sweep that ran
PCA alone would have charged all of it to the dimensions. So three families are run at every
width, and two full-width arms exist purely to attribute the loss:

- **`svd_k`** — uncentred, projecting onto the top `k` right singular vectors. `svd_1024` is a
  pure rotation of the deployed data and must therefore score what `identity_1024` scores; it
  is the plumbing check, and if it drifts nothing else here is readable.
- **`pca_k`** — the same, after subtracting the mean. `pca_1024` is also a pure rotation, of
  *centred* data, so whatever it loses against `identity_1024` is the cost of the subtraction
  alone.
- **`random_k`** — uncentred, onto a random orthonormal subspace. This is the control. If it
  matches the fitted basis then this corpus has no structure worth fitting, and the far
  simpler thing should ship: a fixed matrix, with no fit to run, no 1024-by-k basis to store
  and version, and nothing to refit when the corpus drifts. If it loses, the gap is the part
  of the result that comes from *this corpus* rather than from having `k` dimensions at all.

`random_1024` is deliberately absent — uncentred at full width it is a rotation of exactly
what `identity_1024` holds, so it would be that arm measured a third time.

## What a naive version of this gets wrong

**1. The truth has to be exact.** Every arm is scored against the top ten by exact fp32
cosine at 1024 dimensions, taken with `enable_indexscan` and `enable_bitmapscan` off.
Comparing a reduced HNSW index against the production HNSW index would report the difference
between two errors rather than the error of one.

**2. The queries have to be questions.** Sampling stored embeddings and using them as queries
measures passage->passage similarity. Retrieval is question->passage, which is asymmetric, and
a projection that preserves one need not preserve the other. Every query here is a real
question from `questions.toml`, embedded through the same `TeiClient` the product uses and
put through the same projection as the passages.

**3. Twelve of the forty-three questions are unanswerable, and averaging them in measures
noise.** The corpus cannot answer them; their "exact top ten" is an arbitrary set of ten
passages that happen to be least far away, so asking a reduced index to reproduce that set
measures the faithful reproduction of noise. An earlier report of this kind was inflated by
about 60% that way.

The direction is worth stating carefully, because on *this* metric it runs the other way, and
the report says so with a number rather than repeating the expectation. A question with no
true match sits in a diffuse region where the tenth neighbour is barely closer than the
fiftieth, so any perturbation reorders it and the unanswerable set scores *below* the
answerable one here — `all_questions_recall_at_10` is in every arm so the size of that pull is
visible rather than asserted. Inflating an end-to-end recall figure and deflating an index
recall figure are the same defect seen from two sides: in both cases the number moves for a
reason that has nothing to do with retrieval quality. So the answerable questions are the
headline, and the unanswerable ones are reported beside them, separately, and never averaged
in.

The split is *answerable vs not*, which is a different rule from `harness.py`'s
`HEADLINE_TYPES`, and the difference is deliberate rather than drift. `harness.py` scores what
reaches a reader's page, so it excludes `identifier` and `table` because those are diagnostics
on the lexical half and on extraction. This sweep scores whether a reduced index finds the
same neighbours as an exact one. An `identifier` question has real sources and a real exact
top ten, so it belongs here; only a question with no correct passage at all has no target to
reproduce.

**4. The mean is not the verdict.** A projection that keeps recall at 0.99 on average while
destroying one question is not a smaller index, it is a worse product. The worst single
question is reported at every width beside the mean, and it is named. That statistic decided
the binary-quantisation verdict in `quantisation.json`, and nearly decided it wrongly.

## What is measured, and what is not

Index recall, not end-to-end recall. What a reader sees is the page after fusion and the
cross-encoder, and both can repair — or fail to repair — what the index lost. Index recall is
therefore an upper bound on the damage, not the damage; a width that is unbroken here is
worth taking to `end_to_end` in `quantisation.py`, and a width that is broken here does not
need to be.

## What runs where

Everything this builds lives in one schema, `zenith_dims`, created at the start and dropped in
a `finally`; the run prints its own verification that neither the schema nor any new
`SECURITY DEFINER` function survived it. **`chunk_embeddings` is read and never written.** The
corpus is the user's real one: 42 documents, 13,549 passages. No production table gains a
column, an index or a row, there is no migration here, and nothing is `SECURITY DEFINER`.

The scratch copies carry no RLS policy and no join to `chunks`, so a latency here is not
comparable to one in `latency.json`. It is comparable to the other arms in this report, and
even that only loosely: this database is shared with two other sweeps while this runs, so
recall is trustworthy and latency is contended. Recall is immune to a busy machine; latency
is not.

**`numpy` is required and is not a runtime dependency.** It is declared in the `eval`
dependency group of `pyproject.toml`, never in `[project.dependencies]`, because nothing under
`app/` imports it and `Dockerfile.backend` builds with `uv sync --no-dev`, so the shipped
image does not grow for a measurement. The fit is two symmetric eigendecompositions of a
1024x1024 matrix — seconds of BLAS, and days of pure Python, which is why the dependency
exists rather than being argued away. It is checked for at the top of the run, not assumed.

    docker compose exec -T api python -m eval dimensions [--targets 512,384,256,192,128]
"""

import asyncio
import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.core.config import settings
from app.core.hardware import active as active_profile
from app.features.embeddings.client import DIMENSION, MODEL, VERSION, TeiClient
from app.features.retrieval.search import CANDIDATES
from eval.harness import installation
from eval.questions import Question, load_questions

REPORT = Path(__file__).parent / "dimensions.json"

#: One schema, one name, dropped in a `finally`. Two other sweeps hold `zenith_coarse` and
#: `zenith_ivf` in this database at the same time; nothing here touches a name it does not own.
SCHEMA = "zenith_dims"

#: The production index's parameters, so every arm is the index the installation runs at a
#: different width rather than a differently-tuned cousin.
M = 16
EF_CONSTRUCTION = 64

#: Target widths, in components. 1024 is the deployed width and is built here rather than read
#: off the live index, because a live index carrying ingestion churn reads larger per vector
#: than a freshly built one and every ratio in this report is fresh-against-fresh.
TARGETS = (512, 384, 256, 192, 128)

#: Recall depth. Ten is the depth a reader's page is drawn from and the depth published
#: dimension-reduction figures use; `CANDIDATES` is what the dense half of the product actually
#: asks the index for, so it is carried alongside. The headline is at ten.
DEPTHS = (10, CANDIDATES)

#: Fixed, so the control is the same control between runs. A random projection that moved
#: every run would make the PCA gap unreadable — the reader could not tell a real gap from a
#: lucky draw.
SEED = 20260829

#: Repeats per query per arm; the fastest is kept. The question is what the scan costs, not
#: what the container happened to be doing — and while this runs, two other sweeps are doing
#: something.
REPEATS = 3

#: Passages per document is measured at run time — 322.6 on this corpus — and never taken from
#: a constant, because it is exactly the sort of figure that rots in a file. This is a second,
#: lower assumption reported beside it: this corpus is long legal and standards PDFs, a
#: corporate mix of memos, contracts and slide decks is shorter, and a reader sizing a machine
#: deserves both rather than one number presented as universal.
CORPORATE_PASSAGES_PER_DOCUMENT = 80.0

#: Corpus sizes to project the measured bytes per vector to, in documents.
DOCUMENT_PROJECTIONS = (100_000, 1_000_000, 10_000_000)


@dataclass(frozen=True, slots=True)
class Arm:
    """One projection, at one width, with its own table and its own index."""

    #: Report key: `identity_1024`, `svd_256`, `pca_256`, `random_256`.
    key: str
    #: `identity`, `svd`, `pca` or `random`.
    method: str
    dim: int

    @property
    def basis(self) -> str | None:
        """Which fitted basis this arm projects onto. `None` stores the vector unchanged."""
        return None if self.method == "identity" else self.method

    @property
    def centred(self) -> bool:
        """Whether the corpus mean is subtracted before projecting.

        Only textbook PCA does. This is a field rather than an implementation detail because
        it turned out to be the single largest effect this sweep measured — see `_arms`.
        """
        return self.method == "pca"

    @property
    def table(self) -> str:
        return f"{SCHEMA}.arm_{self.key}"

    @property
    def index(self) -> str:
        return f"ix_arm_{self.key}"


def _arms(targets: tuple[int, ...]) -> tuple[Arm, ...]:
    """Four families, and two full-width arms that exist only to attribute the loss.

    **`identity_1024`** is the deployed representation: `halfvec(1024)` under HNSW, built fresh
    here so every byte ratio in the report is fresh-against-fresh. It is not perfect against
    exact retrieval and is not meant to be — HNSW is approximate, and the gap between this arm
    and 1.0 is the cost the installation already pays before a single dimension is dropped.

    **`svd_k`** projects onto the top `k` right singular vectors of the *uncentred* corpus
    matrix. **`pca_k`** subtracts the corpus mean first, which is what makes it PCA rather than
    a truncated SVD. **`random_k`** projects onto a random orthonormal subspace, uncentred, and
    is the control: if it matches `svd_k` then this corpus has no structure worth fitting and
    a fixed matrix — no fit, no stored basis, no refit when the corpus drifts — is the simpler
    thing to ship.

    Both full-width arms are rotations, and cosine is invariant under rotation, so neither can
    lose anything to *dimensions*. What they can lose is everything else, and that is the point
    of having them:

    - **`svd_1024`** should score what `identity_1024` scores. It is a plumbing check. If it
      does not, the projection is wrong and no number below means anything.
    - **`pca_1024`** should score what `identity_1024` scores too, and does not — centring is
      not a rotation. It changes every norm, and cosine is a function of the norms, so a
      centred index ranks by a different metric and finds different neighbours. Without this
      arm that loss would be charged to the dimensions PCA dropped, and the report would
      conclude that 1024 -> 256 is expensive when what is expensive is the subtraction.

    `random_1024` is deliberately absent: uncentred, at full width, it is a rotation of exactly
    the data `identity_1024` holds, so it would be that arm measured a third time.
    """
    return (
        Arm(key=f"identity_{DIMENSION}", method="identity", dim=DIMENSION),
        Arm(key=f"svd_{DIMENSION}", method="svd", dim=DIMENSION),
        Arm(key=f"pca_{DIMENSION}", method="pca", dim=DIMENSION),
        *(
            Arm(key=f"{method}_{dim}", method=method, dim=dim)
            for method in ("svd", "pca", "random")
            for dim in targets
        ),
    )


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def _split(scorable: list[Question]) -> tuple[list[Question], list[Question], list[str]]:
    """Answerable-and-present, unanswerable, and the answerable ones this corpus is missing.

    `installation()` already drops a question whose document was never uploaded, on
    `live.py`'s rule: scoring it would measure which PDFs happen to be present rather than how
    well retrieval ranks. That rule is right here for a second reason — a question whose
    document is absent has no correct passage in this index, which makes its exact top ten the
    same arbitrary set as an unanswerable question's, and just as inflating.

    The excluded ids are returned rather than dropped quietly, because the count of scored
    questions is the resolution of every number in this report and a reader has to be able to
    see it move.
    """
    present = {question.id for question in scorable}
    every = load_questions()
    unanswerable = [question for question in every if not question.sources]
    missing = [question.id for question in every if question.sources and question.id not in present]
    return scorable, unanswerable, missing


async def _corpus(conn: AsyncConnection, space: Any) -> tuple[list[str], Any]:
    """Every stored embedding, as fp32, in a stable order.

    Pulled over the wire and fitted in this process rather than in SQL. Postgres has no
    eigensolver and the alternative — power iteration with deflation, one round trip per
    component per step — is hundreds of queries over the whole corpus to compute something BLAS
    does in three seconds once the matrix is here. 13,549 x 1024 fp32 is 55 MB; the transfer is
    about a second on a local socket.

    Ordered by `chunk_id` so a rerun projects the same rows in the same order, which is what
    makes two runs of this file comparable.
    """
    import numpy as np

    rows = (
        await conn.execute(
            text(
                "SELECT chunk_id, embedding::text FROM chunk_embeddings "
                "WHERE embedding_model = :model AND embedding_version = :version "
                "ORDER BY chunk_id"
            ),
            {"model": space.model, "version": space.version},
        )
    ).all()
    matrix = np.empty((len(rows), DIMENSION), dtype=np.float32)
    for position, row in enumerate(rows):
        matrix[position] = np.fromstring(row[1][1:-1], sep=",", dtype=np.float32)
    return [str(row[0]) for row in rows], matrix


def _fit(matrix: Any) -> tuple[Any, dict[str, Any], dict[str, list[float]]]:
    """The corpus mean, one basis per family, and the spectrum each family reads off.

    Both fitted bases come from an `eigh` of a 1024x1024 symmetric matrix rather than an SVD of
    the data. Same answer, and the cost does not care whether there are 13,549 rows or a
    million: `svd` is the eigenbasis of the uncentred second moment `X'X / n`, `pca` is the
    eigenbasis of the covariance `Xc'Xc / (n-1)`. They differ by exactly one subtraction, which
    is the comparison this sweep exists to make.

    Every basis is **nested**: the first `k` columns are exactly the basis that would have been
    fitted for `k`. That is what makes six widths one fit rather than six, and it is a property
    of the eigendecomposition rather than a shortcut — the leading eigenvectors do not change
    when you ask for fewer of them. It holds for the random control too, since the first `k`
    columns of an orthonormal matrix are an orthonormal basis of a random `k`-subspace.

    The control is orthonormalised rather than left as raw Gaussian columns. A Gaussian matrix
    distorts distances slightly *more* than an orthogonal one, and the control has to lose to a
    fitted basis on structure rather than on conditioning, or the gap is not the thing a reader
    will take it for.
    """
    import numpy as np

    rows = len(matrix)
    mean = matrix.mean(axis=0)
    centred = matrix - mean

    bases: dict[str, Any] = {}
    spectra: dict[str, list[float]] = {}
    for family, moment in (
        ("svd", (matrix.T @ matrix) / rows),
        ("pca", (centred.T @ centred) / (rows - 1)),
    ):
        values, vectors = np.linalg.eigh(moment)
        order = np.argsort(values)[::-1]
        values, basis = values[order], vectors[:, order]
        bases[family] = basis.astype(np.float32)
        total = float(values.sum())
        spectra[family] = [float(values[:k].sum() / total) for k in range(1, DIMENSION + 1)]

    generator = np.random.default_rng(SEED)
    control, _ = np.linalg.qr(generator.standard_normal((DIMENSION, DIMENSION), dtype=np.float32))
    bases["random"] = control.astype(np.float32)
    return mean, bases, spectra


def _project(matrix: Any, mean: Any, basis: Any | None, dim: int, centred: bool) -> Any:
    """The one transform, applied to passages and to queries alike.

    `basis` of `None` is the identity arm: the stored vector as it is, which is what the
    installation indexes today.

    When the family centres, the mean is fitted on passages and subtracted from queries too.
    That is not an oversight — it is the definition of the map. A projection is only a
    projection if both sides go through it; centring the passages and not the queries would
    compare points in two different affine frames, and the resulting recall would be a bug
    rather than a finding.
    """
    if basis is None:
        return matrix
    source = matrix - mean if centred else matrix
    return source @ basis[:, :dim]


def _literals(matrix: Any) -> list[str]:
    """Rows as pgvector literals.

    `tolist()` first: it is one C-level conversion of the whole array, and `repr` on a Python
    float is the shortest string that round-trips. Formatting per element with numpy scalars is
    about twenty times slower for the same text.
    """
    return ["[" + ",".join(map(repr, row)) + "]" for row in matrix.astype("float32").tolist()]


async def _build(
    conn: AsyncConnection, arm: Arm, ids: list[str], vectors: list[str]
) -> dict[str, object]:
    """One table, one HNSW index, and what that index costs per vector.

    Stored as `halfvec(k)`, because fp16 is the deployed precision since migration 0025 and
    the point of this sweep is the lever that multiplies with that one, not a second
    measurement of it.

    Inserted through `unnest` of two arrays rather than row by row: one round trip per two
    thousand rows instead of two thousand, and the driver casts the literals once at the
    boundary.
    """
    await conn.execute(
        text(f"CREATE TABLE {arm.table} (chunk_id uuid PRIMARY KEY, e halfvec({arm.dim}))")
    )
    batch = 2000
    for start in range(0, len(ids), batch):
        await conn.execute(
            text(
                f"INSERT INTO {arm.table} (chunk_id, e) "
                f"SELECT * FROM unnest(CAST(:i AS uuid[]), CAST(:v AS halfvec({arm.dim})[]))"
            ),
            {"i": ids[start : start + batch], "v": vectors[start : start + batch]},
        )
    await conn.execute(text(f"ANALYZE {arm.table}"))

    started = time.perf_counter()
    await conn.execute(
        text(
            f"CREATE INDEX {arm.index} ON {arm.table} USING hnsw (e halfvec_cosine_ops) "
            f"WITH (m = {M}, ef_construction = {EF_CONSTRUCTION})"
        )
    )
    build_ms = (time.perf_counter() - started) * 1000
    size_bytes = int(
        (
            await conn.execute(
                text("SELECT pg_relation_size(:name)"), {"name": f"{SCHEMA}.{arm.index}"}
            )
        ).scalar_one()
    )
    return {
        "dim": arm.dim,
        "method": arm.method,
        "index_bytes": size_bytes,
        "bytes_per_vector": round(size_bytes / len(ids), 1),
        "build_ms": round(build_ms),
    }


async def _plan(conn: AsyncConnection, statement: str, params: dict[str, object]) -> str:
    """The node types Postgres chose, joined into one line.

    Recorded rather than trusted. Two of the settings this file depends on are planner hints
    rather than commands — `enable_indexscan = off` raises a cost, it does not forbid a plan —
    and a hint that silently failed would turn every number in this report into a comparison of
    something against itself. The plan is in the report so that the claim "the truth was a
    sequential scan and the arms used their own HNSW index" is a fact a reader can check
    instead of a sentence a docstring asserts.
    """
    rows = await conn.execute(text(f"EXPLAIN (COSTS off) {statement}"), params)
    # Truncated per line: a sort key on a vector distance prints the whole 1024-component
    # literal, which would be most of this report by weight and none of it by content.
    return " | ".join(line.strip()[:60] for (line,) in rows.fetchall() if line and line.strip())


async def _truth(
    conn: AsyncConnection, space: Any, vectors: list[str]
) -> tuple[list[list[UUID]], str]:
    """Exact fp32 neighbours at 1024 dimensions, with every index refused.

    Read straight off `chunk_embeddings` rather than off a copy: the ground truth for "what did
    the reduced index miss" is the answer the unreduced, unapproximated, unquantised column
    gives, and that column is in the production table. `SET TRANSACTION READ ONLY` is not on
    this connection — the same transaction builds the scratch schema — so the guarantee here is
    that the statement is a `SELECT`, and the schema-drop verification at the end of the run is
    what backs it.

    `enable_indexscan = off` is not belt and braces. The production HNSW index would otherwise
    serve this query, and an approximate truth makes every recall number below the difference
    between two errors instead of the error of one.
    """
    await conn.execute(text("SET LOCAL enable_indexscan = off"))
    await conn.execute(text("SET LOCAL enable_bitmapscan = off"))
    statement = (
        "SELECT chunk_id FROM chunk_embeddings "
        "WHERE embedding_model = :model AND embedding_version = :version "
        "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
    )
    plan = await _plan(
        conn,
        statement,
        {"model": space.model, "version": space.version, "q": vectors[0], "k": max(DEPTHS)},
    )
    truth: list[list[UUID]] = []
    for vector in vectors:
        rows = await conn.execute(
            text(
                "SELECT chunk_id FROM chunk_embeddings "
                "WHERE embedding_model = :model AND embedding_version = :version "
                "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
            ),
            {"model": space.model, "version": space.version, "q": vector, "k": max(DEPTHS)},
        )
        truth.append([row.chunk_id for row in rows])
    return truth, plan


async def _score(
    conn: AsyncConnection,
    arm: Arm,
    labels: list[str],
    queries: list[str],
    truth: list[list[UUID]],
    ef_search: int,
) -> dict[str, object]:
    """One arm against the exact neighbours, question by question.

    Per-question recall is kept and reported in full, not just reduced to a mean. The mean is
    the number a summary quotes; the per-question map is what lets someone disbelieve it, and
    the worst entry in it is the number that decides whether a width is shippable.
    """
    # `_truth` ran `enable_indexscan = off` on this same connection, and `SET LOCAL` lasts to
    # the end of the transaction rather than to the end of the statement. Without these two
    # lines every arm would be answered by a sequential scan over its own table, which is exact
    # — so every arm would score a perfect recall against a truth computed the same way, and the
    # sweep would report that dimensions are free at every width. The plan below is what proves
    # it did not happen.
    await conn.execute(text("SET LOCAL enable_indexscan = on"))
    await conn.execute(text("SET LOCAL enable_bitmapscan = on"))
    await conn.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))
    statement = (
        f"SELECT chunk_id FROM {arm.table} ORDER BY e <=> CAST(:q AS halfvec({arm.dim})) LIMIT :k"
    )
    plan = await _plan(conn, statement, {"q": queries[0], "k": max(DEPTHS)})
    overlaps: dict[int, list[float]] = {depth: [] for depth in DEPTHS}
    top1 = 0
    times: list[float] = []

    for query, exact_ids in zip(queries, truth, strict=True):
        got: list[UUID] = []
        fastest: float | None = None
        for _ in range(REPEATS):
            started = time.perf_counter()
            rows = await conn.execute(text(statement), {"q": query, "k": max(DEPTHS)})
            got = [row.chunk_id for row in rows]
            elapsed = (time.perf_counter() - started) * 1000
            fastest = elapsed if fastest is None else min(fastest, elapsed)
        times.append(fastest or 0.0)
        top1 += 1 if got and exact_ids and got[0] == exact_ids[0] else 0
        for depth in DEPTHS:
            wanted = set(exact_ids[:depth])
            overlaps[depth].append(len(set(got[:depth]) & wanted) / len(wanted))

    at_ten = overlaps[10]
    ranked = sorted(zip(labels, at_ten, strict=True), key=lambda pair: pair[1])
    return {
        "plan": plan,
        "plan_uses_index": arm.index in plan,
        **{
            f"index_recall_at_{depth}": round(statistics.mean(overlaps[depth]), 4)
            for depth in DEPTHS
        },
        **{f"worst_question_at_{depth}": round(min(overlaps[depth]), 4) for depth in DEPTHS},
        "worst_question_id": ranked[0][0],
        "questions_scored": len(labels),
        "questions_perfect_at_10": sum(1 for value in at_ten if value >= 1.0),
        "questions_below_0_9_at_10": sum(1 for value in at_ten if value < 0.9),
        "questions_below_0_5_at_10": sum(1 for value in at_ten if value < 0.5),
        "top1_preserved": round(top1 / len(labels), 4),
        "five_worst_at_10": [[label, round(value, 4)] for label, value in ranked[:5]],
        "recall_at_10_by_question": {
            label: round(value, 4) for label, value in zip(labels, at_ten, strict=True)
        },
        "median_ms": round(statistics.median(times), 3),
        "p95_ms": round(_percentile(times, 0.95), 3),
    }


def _footprint(bytes_per_vector: float, passages_per_document: float) -> dict[str, str]:
    """Resident index at corpus sizes this machine will never hold.

    Straight arithmetic on the measured bytes per vector. HNSW's per-vector cost is `m` and the
    width, neither of which changes with N, so the linear projection is sound for *size*. It
    says nothing about build time or about recall at those sizes, both of which do change with
    N and neither of which is measured here — 13,549 vectors is not the regime, and a
    dimension-reduction result at 13.5k is a statement about this corpus's structure, not about
    a hundred million passages.
    """
    return {
        str(documents): f"{bytes_per_vector * documents * passages_per_document / 1024**3:.1f}"
        for documents in DOCUMENT_PROJECTIONS
    }


def _summarise(arm: dict[str, Any], identity: dict[str, Any]) -> None:
    """Add the two figures the verdict is read off, computed rather than left to a reader.

    **`all_questions_recall_at_10`** is what the headline would have been had the split not
    been made. It is here so the cost of averaging the unanswerable questions in is a number in
    the report rather than a claim in a docstring — and, on this metric, it turned out to point
    the opposite way from the expectation that motivated the split.

    **`versus_identity`** is the answerable set compared question by question against
    `identity_1024`, which is the deployed index and is itself approximate. Nothing here is
    scored against a perfect baseline, because the installation does not have one: at ef_search
    100 the deployed HNSW already returns five of the exact top ten for its worst question.
    "Unbroken" therefore means "no worse than what is already shipped", and `questions_worse`
    is the count that answers it — a width that halves memory and drops one more question is
    not a smaller index, it is a worse product, and this is the field that says which happened.
    """
    answerable = arm["answerable"]["recall_at_10_by_question"]
    unanswerable = arm["unanswerable"]["recall_at_10_by_question"]
    every = [*answerable.values(), *unanswerable.values()]
    arm["all_questions_recall_at_10"] = round(sum(every) / len(every), 4)

    baseline = identity["answerable"]["recall_at_10_by_question"]
    deltas = {key: round(value - baseline[key], 4) for key, value in answerable.items()}
    worse = {key: value for key, value in deltas.items() if value < 0}
    arm["versus_identity"] = {
        "mean_delta_at_10": round(
            arm["answerable"]["index_recall_at_10"] - identity["answerable"]["index_recall_at_10"],
            4,
        ),
        "questions_worse": len(worse),
        "questions_worse_by_more_than_0_1": sum(1 for value in worse.values() if value < -0.1),
        "questions_better": sum(1 for value in deltas.values() if value > 0),
        "largest_single_loss": round(min(deltas.values()), 4) if deltas else 0.0,
        "largest_single_loss_question": min(deltas, key=lambda key: deltas[key])
        if deltas
        else None,
    }


async def _run(targets: tuple[int, ...]) -> int:
    try:
        import numpy
    except ModuleNotFoundError:
        # Checked, not assumed, and it fails here rather than four minutes into a run: the
        # api image ships without numpy on purpose and there is no reason for it to grow.
        print(
            "numpy is required and is not installed in this container.\n"
            "It is declared in the `eval` dependency group of pyproject.toml, never in the "
            "runtime dependencies. Install it into the running image with:\n"
            "  uv pip install --python /app/.venv/bin/python 'numpy>=2.1'"
        )
        return 1
    numpy_version = str(numpy.__version__)

    where = await installation()
    answerable, unanswerable, missing = _split(where.questions)
    if not answerable:
        print("No question's document is in this corpus — nothing to measure.")
        return 1

    hardware = active_profile()
    ef_search = hardware.hnsw_ef_search
    arms = _arms(targets)

    print(
        f"{where.space.n} embeddings, {len(answerable)} answerable and "
        f"{len(unanswerable)} unanswerable questions, profile {hardware.name}, "
        f"ef_search {ef_search}"
    )
    if missing:
        print(f"excluded, document not in this corpus: {', '.join(missing)}")

    embedder = TeiClient(profile=hardware)
    embeddings = [
        await embedder.embed_query(question.question) for question in answerable + unanswerable
    ]
    print(f"{len(embeddings)} questions embedded through {MODEL} {VERSION}\n", flush=True)

    owner = create_async_engine(settings.database_owner_url)
    measured: dict[str, dict[str, object]] = {}
    spectra: dict[str, list[float]] = {}
    rows = documents = chunks = production_index_bytes = 0

    try:
        async with owner.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
            await conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
            # The default 64 MB makes HNSW construction spill and crawl, which would report a
            # build time for the memory setting rather than for the width.
            await conn.execute(text("SET maintenance_work_mem = '512MB'"))

            counted = (
                await conn.execute(text("SELECT count(DISTINCT document_id), count(*) FROM chunks"))
            ).one()
            documents, chunks = int(counted[0]), int(counted[1])
            production_index_bytes = int(
                (
                    await conn.execute(
                        text(
                            "SELECT coalesce(pg_relation_size(to_regclass("
                            "'ix_chunk_embeddings_hnsw_half')), 0)"
                        )
                    )
                ).scalar_one()
            )

            import numpy as np

            started = time.perf_counter()
            ids, matrix = await _corpus(conn, where.space)
            rows = len(ids)
            print(f"  {rows} vectors read in {time.perf_counter() - started:.1f} s", flush=True)

            started = time.perf_counter()
            mean, bases, spectra = _fit(matrix)
            print(f"  fitted in {time.perf_counter() - started:.1f} s", flush=True)

            queries = np.asarray(embeddings, dtype=np.float32)

            truth, truth_plan = await _truth(conn, where.space, _literals(queries))
            print(f"  exact fp32 truth, plan: {truth_plan}\n", flush=True)

            for arm in arms:
                built = await _build(
                    conn,
                    arm,
                    ids,
                    _literals(
                        _project(matrix, mean, bases.get(arm.basis or ""), arm.dim, arm.centred)
                    ),
                )
                projected = _literals(
                    _project(queries, mean, bases.get(arm.basis or ""), arm.dim, arm.centred)
                )
                split = len(answerable)
                measured[arm.key] = {
                    **built,
                    "answerable": await _score(
                        conn,
                        arm,
                        [question.id for question in answerable],
                        projected[:split],
                        truth[:split],
                        ef_search,
                    ),
                    "unanswerable": await _score(
                        conn,
                        arm,
                        [question.id for question in unanswerable],
                        projected[split:],
                        truth[split:],
                        ef_search,
                    ),
                }
                _summarise(measured[arm.key], measured[f"identity_{DIMENSION}"])
                head: dict[str, Any] = measured[arm.key]["answerable"]  # type: ignore[assignment]
                print(
                    f"    {arm.key:<14} {built['bytes_per_vector']:>7} B/vec  "
                    f"r@10 {head['index_recall_at_10']:.4f}  "
                    f"worst {head['worst_question_at_10']:.4f}  "
                    f"({head['worst_question_id']})",
                    flush=True,
                )

        baseline = float(measured[f"identity_{DIMENSION}"]["bytes_per_vector"])  # type: ignore[arg-type]
        passages_per_document = round(chunks / documents, 1) if documents else 0.0
        report: dict[str, Any] = {
            "corpus": {
                "documents": documents,
                "chunks": chunks,
                "vectors_indexed": rows,
                "passages_per_document": passages_per_document,
                "production_index_bytes": production_index_bytes,
                "production_bytes_per_vector": round(production_index_bytes / chunks, 1)
                if chunks
                else None,
                "production_note": (
                    "`production_bytes_per_vector` is the live halfvec(1024) index, which "
                    "carries ingestion churn and therefore reads larger per vector than the "
                    "freshly built `identity_1024` arm below. Every ratio in this report is "
                    "taken against that fresh arm, so it is fresh-against-fresh."
                ),
            },
            "questions": {
                "answerable_scored": len(answerable),
                "unanswerable_scored": len(unanswerable),
                "excluded_document_absent": missing,
                "split_note": (
                    "The headline is the answerable set. An unanswerable question has no "
                    "correct passage, so its exact top ten is an arbitrary set of ten "
                    "distant passages; those are easy for any projection to reproduce, they "
                    "score high, and averaging them in inflates the result. They are measured "
                    "and reported because a reduced index that scrambles them is still a "
                    "signal, not because they belong in the number."
                ),
            },
            "method": {
                "model": MODEL,
                "version": VERSION,
                "source_dimension": DIMENSION,
                "stored_as": "halfvec(k)",
                "targets": list(targets),
                "seed": SEED,
                "repeats": REPEATS,
                "hnsw": {"m": M, "ef_construction": EF_CONSTRUCTION, "ef_search": ef_search},
                "profile": hardware.name,
                "numpy": numpy_version,
                "truth": (
                    "Exact fp32 cosine at 1024 dimensions over chunk_embeddings, with "
                    "enable_indexscan and enable_bitmapscan off. Ground truth for every arm, "
                    "including identity_1024."
                ),
                "truth_plan": truth_plan,
                "truth_is_sequential": "Seq Scan" in truth_plan,
                "fit": (
                    "Fitted on this corpus's own passage embeddings. `svd` is the eigenbasis "
                    "of the uncentred second moment X'X/n; `pca` is the eigenbasis of the "
                    "covariance Xc'Xc/(n-1), which is the same thing after subtracting the "
                    "corpus mean. `random` is an orthonormalised Gaussian basis at a fixed "
                    "seed, uncentred. Every basis is nested, so svd_256 is the first 256 "
                    "columns of the same fit as svd_512. The same transform — mean where the "
                    "family centres, basis always — is applied to the query vectors."
                ),
                "families": {
                    "identity": "the stored vector, unchanged: what the installation indexes",
                    "svd": "uncentred projection onto the corpus's top singular directions",
                    "pca": "the same, after subtracting the corpus mean",
                    "random": "uncentred projection onto a random orthonormal subspace",
                },
            },
            "explained_variance_ratio": {
                "note": (
                    "Fraction of the family's own spectrum kept at each width: `svd` is the "
                    "uncentred second moment, `pca` the covariance. It is the information "
                    "argument for a width, and it is reported beside recall precisely because "
                    "the two do not have to agree — variance retained is not neighbours "
                    "retained, and only the second one is the product."
                ),
                **{
                    family: {str(dim): round(spectrum[dim - 1], 4) for dim in (*targets, DIMENSION)}
                    for family, spectrum in spectra.items()
                },
            },
            "arms": measured,
            "bytes_per_vector": {key: value["bytes_per_vector"] for key, value in measured.items()},
            "smaller_than_identity_1024": {
                key: round(baseline / float(value["bytes_per_vector"]), 2)  # type: ignore[arg-type]
                for key, value in measured.items()
            },
            "resident_index_gib": {
                "note": (
                    "GiB of HNSW index at each document count, from the measured bytes per "
                    "vector. The passages-per-document assumption is stated in each block "
                    "because it is what a reader would otherwise quote a footprint without."
                ),
                "this_corpus": {
                    "passages_per_document": passages_per_document,
                    "by_arm": {
                        key: _footprint(
                            float(value["bytes_per_vector"]),  # type: ignore[arg-type]
                            passages_per_document,
                        )
                        for key, value in measured.items()
                    },
                },
                "corporate_mix": {
                    "passages_per_document": CORPORATE_PASSAGES_PER_DOCUMENT,
                    "by_arm": {
                        key: _footprint(
                            float(value["bytes_per_vector"]),  # type: ignore[arg-type]
                            CORPORATE_PASSAGES_PER_DOCUMENT,
                        )
                        for key, value in measured.items()
                    },
                },
            },
            "caveats": [
                "Index recall, not end-to-end recall. Fusion with the lexical half and the "
                "cross-encoder sit downstream and can repair or fail to repair what the index "
                "lost, so a number here is an upper bound on the damage rather than the "
                "damage. A width that survives here is what quantisation.py's end_to_end "
                "should be pointed at next.",
                "The headline is scored over a few dozen questions, so it cannot resolve a "
                "difference finer than one question at one rank. Read 'unchanged' as 'not "
                "changed by more than that', not as equality.",
                "Latencies were taken while two other sweeps were building indexes against "
                "this database. Recall is immune to a contended machine; latency is not, and "
                "these figures should be read as an ordering at best.",
                "The scratch tables carry no RLS policy and no join to chunks, so latencies "
                "here are not comparable to latency.json or ef-search.json.",
                "13,549 vectors is not the regime the 1M-document projection describes. The "
                "byte projection is linear and sound; the recall figures are a statement "
                "about this corpus's structure at this size, and a PCA basis fitted on 42 "
                "documents may not be the basis a thousand tenants' documents would give.",
                "The PCA basis is data the index depends on. Shipping this would mean storing "
                "a 1024xk matrix, applying it in the ingestion path and in the query path, and "
                "deciding what happens when the corpus drifts far enough that the fit is "
                "stale. None of that is measured here; this sweep only says what the recall "
                "would be if it were done.",
            ],
        }
        REPORT.write_text(json.dumps(report, indent=2) + "\n")
        print(f"\nWritten to {REPORT.name}")
    finally:
        # Unconditional, and verified rather than assumed. The rule this obeys was written
        # after two undeclared `SECURITY DEFINER` functions from an earlier investigation were
        # found still installed, readable by PUBLIC, months later.
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
        print(
            f"{SCHEMA} left behind: {left} (want 0); "
            f"SECURITY DEFINER in public: {definers} (want 7)"
        )
        await owner.dispose()

    return 0


#: How `python -m eval` finds this sweep. Declared here rather than listed in `__main__.py`,
#: so adding a measurement is adding a file and nothing else.
COMMAND = "dimensions"
USAGE = "dimensions [--targets N,N,...]"


def run(targets: tuple[int, ...] = TARGETS) -> int:
    return asyncio.run(_run(targets))


def cli(argv: list[str]) -> int:
    """`--targets N,N,...`, or the default ladder."""
    if "--targets" in argv:
        return run(tuple(int(n) for n in argv[argv.index("--targets") + 1].split(",")))
    return run()
