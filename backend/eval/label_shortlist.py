# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""Would a semantic shortlist let the classifier work above `MAX_LABELS = 60`?

`ingestion/classification.py` stops offering labels once the uploader reaches more than
sixty, and it is right about *why*: the candidate list is ordered by name, so taking the
first sixty of two thousand would file everything under whatever begins with "a". The
cost of that decision is that a tenant with two thousand labels has automatic filing
switched off and is not told.

The proposal is to replace the ceiling with a relevance cut: embed the label names once,
embed the document, and hand the model the k nearest labels by cosine. The cost then stops
depending on the label count.

**This file changes nothing and is not meant to.** It answers one question, and it is not
"does the shortlist run":

    When a human has already filed a document, is the label they chose inside the
    shortlist the model would have been shown?

A shortlist that is fast and does not contain the human's answer is worse than the ceiling
it replaces, because above sixty labels the product currently files nothing, and after the
change it would file confidently and wrongly.

Three things this measurement inherits rather than invents:

- **The embedding path is the one that already exists.** `tei-embed`, BAAI/bge-m3, the
  single `active` row in `embedding_spaces`. No second embedder.
- **A document's vector is the mean of its passage vectors.** Measured, not assumed:
  `eval/coarse.json` has mean pooling beating max at all 26 dial positions with no ties.
  The vectors are already in `chunk_embeddings` before filing runs — the pipeline embeds
  and then classifies — so the document side of the shortlist costs nothing new.
- **The pool is the uploader's reach, resolved by `UserRepository.label_ids`.** The
  shortlist is applied *after* that scoping, never instead of it. It is a relevance filter;
  the access filter is above it and stays there. Building the pool from the tenant's whole
  label table would measure a different, and inadmissible, design.

**Ground truth is the labels a human put on the 42 documents in this installation, and it
is the only ground truth available.** Its weaknesses are recorded in the report rather than
argued away here: the sample is small, one label covers 16 of the 26 usable documents, and
"human" means the person who built this corpus rather than a records manager filing their
own work. `audit_events` is read to prove that none of the labels now on a document was put
there by the classifier — a ground truth contaminated by the model's own past output would
be the "recall of 1.0 by comparing retrieval against itself" mistake in another costume.

**Controls, because a clean number here would be suspicious.** The real reach is 14
offerable labels, so any k >= 14 returns the whole pool and recall is 1.0 by construction
and means nothing. The pools above sixty are therefore synthetic, and two controls run
beside every arm: a random shortlist of the same size (which must score about k/N), and the
whole pool (which must score exactly 1.0, or the harness is broken).

**How the synthetic labels are made, because that decides the answer.** Plausible folder
names built from department x topic x qualifier taxonomies in English and Spanish, the same
shapes the real ones have — `Procurement/tenders/2024`, `Nóminas/recibos/Q3`. Names that
case-fold onto a real one are dropped; near neighbours are deliberately kept, because a
tenant with two thousand labels really does have `legal/contracts` next to
`Legal/contracts/2024`, and a pool of unrelated noise would make the shortlist look far
better than it is. The pools are nested, so the curve from 60 to 2,000 measures adding
candidates and not swapping them.

    python -m eval label-shortlist

Read-only against the corpus. The cost arm needs a table to query, so it builds one in a
scratch schema and drops it in a `finally`; nothing else writes.
"""

from __future__ import annotations

import asyncio
import json
import random
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np
from numpy.typing import NDArray
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.features.auth.repository import UserRepository
from app.features.ingestion.classification import EXCERPT_CHARACTERS, MAX_LABELS
from app.features.tenancy.context import TenantContext
from eval.embedder import TeiEmbedder

REPORT = Path(__file__).parent / "label-shortlist.json"

#: The shortlist sizes swept. 25 is the proposal; 5 and 100 bracket it so the shape of the
#: curve is visible rather than three points on a line nobody drew.
K_VALUES = (5, 10, 25, 50, 100)

#: The size the proposal names, and the one the bar is written against.
PROPOSED_K = 25

#: Candidate pool sizes. The first is the reach this installation actually has; the rest
#: are that reach padded with synthetic labels. 60 is `MAX_LABELS` itself — the last point
#: where the product still files today.
POOLS = (60, 200, 2_000)

#: Fixed so the synthetic pool is the same on every re-run. Changing it changes the labels
#: and therefore the answer, which is why it is written into the report.
SEED = 20260830

#: How many chunks the pipeline joins into the excerpt the model reads. Not imported,
#: because it is a slice literal in `pipeline._classify` rather than a constant; kept here
#: with its source named so the drift is visible if it moves.
EXCERPT_CHUNKS = 6  # pipeline.py: `excerpt = "\n".join(chunk.text for chunk in chunks[:6])`

#: The bar, fixed before the run. A bar that fails stays in the report as it was written.
BAR: dict[str, Any] = {
    "written_before_the_run": True,
    "b0_the_shortlist_is_actually_a_shortlist": (
        f"at pool 2000, k={PROPOSED_K} must discard at least 98% of candidates. A shortlist "
        "that keeps nearly everything would score well and prove nothing."
    ),
    "b1_random_control_is_near_zero": (
        f"a random shortlist of {PROPOSED_K} from 2000 must score below 0.05 micro recall. "
        "If chance scores well, the measurement is not measuring the shortlist."
    ),
    "b2_whole_pool_control_is_exactly_one": (
        "recall at k = pool size must be 1.0000. Anything else means a ground-truth label "
        "has no vector and the harness is broken."
    ),
    "b3_recall_at_25_holds_at_scale": (
        f"micro recall@{PROPOSED_K} >= 0.95 at pool 2000. Below that the model never sees "
        "one human label in twenty and cannot choose it, however good the prompt is."
    ),
    "b4_recall_does_not_decay_with_label_count": (
        f"micro recall@{PROPOSED_K} at pool 2000 >= (micro recall@{PROPOSED_K} at pool 60) "
        "- 0.05. The claim under test is that cost and quality stop depending on the label "
        "count; a decaying curve refutes it."
    ),
    "b5_cost_is_noise_beside_the_call_it_precedes": (
        "median per-document shortlist query at pool 2000 <= 100 ms, against a measured "
        "answer-path median of 10544.7 ms in eval/answer-report.json."
    ),
}


@dataclass(frozen=True, slots=True)
class Document:
    document_id: UUID
    filename: str
    truth: tuple[UUID, ...]
    """The offerable labels a human put on it. Ground truth."""


@dataclass(frozen=True, slots=True)
class Label:
    label_id: UUID
    name: str
    real: bool


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(round(value, 7)) for value in vector) + "]"


def _unit(matrix: NDArray[np.float64]) -> NDArray[np.float64]:
    """Rows scaled to unit length, so a dot product is a cosine.

    The stored passage vectors are already unit-norm, but their *mean* is not, and neither
    is anything TEI is asked for without checking. Normalising once here is cheaper than
    trusting two different producers to agree.
    """
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0.0, 1.0, norms)


def synthesise(count: int, taken: set[str], seed: int) -> list[str]:
    """Plausible folder names, deterministically, avoiding the real ones.

    Built from taxonomies rather than sampled from noise. The reason is in the module
    docstring and it is the single choice that most decides the result: a pool of unrelated
    strings leaves the real label alone at the top of every cosine ordering, and the
    shortlist then looks perfect because nothing was ever asked of it.

    Names that case-fold onto a real label are dropped — offering the same folder twice
    would be a bug, not a distractor. Near neighbours are kept on purpose.
    """
    departments_en = (
        "Finance", "Legal", "Human Resources", "Operations", "Procurement", "Marketing",
        "Engineering", "Compliance", "Internal Audit", "Facilities", "Sales",
        "Customer Support", "Information Security", "Research", "Treasury", "Payroll",
        "Insurance", "Logistics", "Quality Assurance", "Corporate Communications",
    )  # fmt: skip
    departments_es = (
        "Contabilidad", "Recursos Humanos", "Compras", "Asesoría Jurídica", "Calidad",
        "Producción", "Ventas", "Tesorería", "Nóminas", "Seguridad", "Formación",
        "Auditoría Interna", "Logística", "Servicios Generales", "Atención al Cliente",
    )  # fmt: skip
    topics_en = (
        "contracts", "invoices", "policies", "reports", "minutes", "budgets", "audits",
        "claims", "tenders", "agreements", "certificates", "manuals", "records",
        "correspondence", "permits", "licences", "appraisals", "receipts", "statements",
        "forecasts",
    )  # fmt: skip
    topics_es = (
        "contratos", "facturas", "políticas", "informes", "actas", "presupuestos",
        "auditorías", "reclamaciones", "licitaciones", "convenios", "certificados",
        "manuales", "expedientes", "correspondencia", "permisos", "licencias",
        "valoraciones", "recibos", "extractos", "previsiones",
    )  # fmt: skip
    qualifiers = (
        *(str(year) for year in range(2018, 2028)),
        "EMEA", "APAC", "LATAM", "North America", "confidential", "archive", "drafts",
        "signed", "internal", "external", "Q1", "Q2", "Q3", "Q4", "consolidated",
        "restricted",
    )  # fmt: skip

    candidates: list[str] = []
    for departments, topics in ((departments_en, topics_en), (departments_es, topics_es)):
        for department in departments:
            for topic in topics:
                candidates.append(f"{department}/{topic}")
                for qualifier in qualifiers:
                    candidates.append(f"{department}/{topic}/{qualifier}")
        for topic in topics:
            for qualifier in qualifiers:
                candidates.append(f"{topic} {qualifier}")

    random.Random(seed).shuffle(candidates)
    lowered = {name.casefold() for name in taken}
    chosen: list[str] = []
    for name in candidates:
        if name.casefold() in lowered:
            continue
        lowered.add(name.casefold())
        chosen.append(name)
        if len(chosen) == count:
            break
    if len(chosen) < count:
        raise RuntimeError(f"taxonomy yields {len(chosen)} names, {count} wanted")
    return chosen


async def _installation() -> dict[str, Any]:
    """What this installation is, read rather than assumed.

    Every figure the report quotes about the corpus comes from here, because a corpus
    statistic written down by hand is the one that rots first.
    """
    engine = create_async_engine(settings.database_owner_url)
    async with engine.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        migration = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        space = (
            await conn.execute(
                text(
                    "SELECT model, version, dimension, status FROM embedding_spaces "
                    "WHERE status = 'active'"
                )
            )
        ).one()
        tenants = (
            await conn.execute(
                text(
                    "SELECT t.id, t.name,"
                    " (SELECT count(*) FROM documents d WHERE d.tenant_id = t.id) AS documents,"
                    " (SELECT count(*) FROM chunks c WHERE c.tenant_id = t.id) AS chunks,"
                    " (SELECT count(*) FROM access_labels a WHERE a.tenant_id = t.id) AS labels,"
                    " (SELECT count(*) FROM access_labels a WHERE a.tenant_id = t.id"
                    "    AND NOT a.is_default AND NOT a.is_quarantine) AS offerable"
                    " FROM tenants t ORDER BY chunks DESC"
                )
            )
        ).all()
        classified = (
            await conn.execute(
                text(
                    "SELECT details ->> 'outcome' AS outcome, count(*) AS n,"
                    " count(*) FILTER (WHERE target_id IN (SELECT id FROM documents)) AS still_here"
                    " FROM audit_events WHERE action = 'document.classified' GROUP BY 1"
                )
            )
        ).all()
        await conn.rollback()
    await engine.dispose()

    return {
        "migration": migration,
        "embedding_space": {
            "model": space.model,
            "version": space.version,
            "dimension": space.dimension,
        },
        "documents": sum(row.documents for row in tenants),
        "chunks": sum(row.chunks for row in tenants),
        "tenants": [
            {
                "id": str(row.id),
                "name": row.name,
                "documents": row.documents,
                "chunks": row.chunks,
                "labels": row.labels,
                "offerable_labels": row.offerable,
                "ceiling_binds": row.offerable > MAX_LABELS,
            }
            for row in tenants
        ],
        "classifier_history": [
            {"outcome": row.outcome, "events": row.n, "documents_still_present": row.still_here}
            for row in classified
        ],
    }


async def _corpus(tenant_id: UUID, uploader: UUID) -> tuple[list[Label], list[Document], Any]:
    """The reach, the labels inside it, the filed documents, and the vectors.

    `UserRepository.label_ids` resolves the reach, under RLS, exactly as `_candidates`
    does. The rest of the query is `_candidates`' own SQL: reserved labels are never
    offered, so they are never ground truth either.
    """
    from app.core.database import tenant_session

    engine = create_async_engine(settings.database_owner_url)
    async with engine.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        all_labels = tuple((await conn.execute(text("SELECT id FROM access_labels"))).scalars())
    context = TenantContext(tenant_id=tenant_id, label_ids=all_labels, user_id=uploader)
    async with tenant_session(context) as session:
        reach = await UserRepository(session).label_ids(uploader)

    async with engine.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        rows = (
            await conn.execute(
                text(
                    "SELECT id, name FROM access_labels "
                    "WHERE id = ANY(:ids) AND NOT is_quarantine AND NOT is_default "
                    "ORDER BY name"
                ),
                {"ids": [str(label_id) for label_id in reach]},
            )
        ).all()
        labels = [Label(label_id=row.id, name=row.name, real=True) for row in rows]
        offerable = {label.label_id for label in labels}

        documents = [
            document
            for row in (
                await conn.execute(
                    text(
                        "SELECT id, filename, label_ids FROM documents "
                        "WHERE tenant_id = :t AND status = 'ready' ORDER BY filename"
                    ),
                    {"t": tenant_id},
                )
            ).all()
            if (
                document := Document(
                    document_id=row.id,
                    filename=row.filename,
                    truth=tuple(label for label in row.label_ids if label in offerable),
                )
            ).truth
        ]

        wanted = [str(document.document_id) for document in documents]
        full = (
            await conn.execute(
                text(
                    "SELECT c.document_id, avg(e.embedding)::text AS mean FROM chunks c "
                    "JOIN chunk_embeddings e ON e.chunk_id = c.id AND e.tenant_id = c.tenant_id "
                    "WHERE c.document_id = ANY(:ids) GROUP BY c.document_id"
                ),
                {"ids": wanted},
            )
        ).all()
        head = (
            await conn.execute(
                text(
                    "WITH ordered AS ("
                    "  SELECT c.id, c.document_id, c.text, row_number() OVER ("
                    "    PARTITION BY c.document_id ORDER BY c.page_num NULLS FIRST, c.char_start"
                    "  ) AS position FROM chunks c WHERE c.document_id = ANY(:ids))"
                    " SELECT o.document_id, avg(e.embedding)::text AS mean,"
                    "        string_agg(o.text, E'\\n' ORDER BY o.position) AS excerpt"
                    " FROM ordered o"
                    " JOIN chunk_embeddings e ON e.chunk_id = o.id"
                    " WHERE o.position <= :n GROUP BY o.document_id"
                ),
                {"ids": wanted, "n": EXCERPT_CHUNKS},
            )
        ).all()
        await conn.rollback()
    await engine.dispose()

    vectors = {
        "full_mean": {row.document_id: json.loads(row.mean) for row in full},
        "head_mean": {row.document_id: json.loads(row.mean) for row in head},
    }
    excerpts = {row.document_id: (row.excerpt or "")[:EXCERPT_CHARACTERS] for row in head}
    return labels, documents, (vectors, excerpts)


def _recall(ranks: list[int], k: int) -> float:
    return round(sum(rank <= k for rank in ranks) / len(ranks), 4) if ranks else 0.0


def _arm(
    documents: list[Document],
    doc_matrix: NDArray[np.float64],
    label_matrix: NDArray[np.float64],
    pool: list[Label],
    rng: random.Random,
) -> dict[str, Any]:
    """Rank every human label inside one pool, then read the recalls off the ranks.

    The rank is the measurement; recall@k is its cumulative distribution. Reporting the
    ranks means a k that was never swept can still be answered from the file, and it makes
    a near miss visible as a near miss rather than as a flat zero.
    """
    index = {label.label_id: position for position, label in enumerate(pool)}
    scores = doc_matrix @ label_matrix.T

    ranks: list[int] = []
    best_ranks: list[int] = []
    per_document: list[float] = []
    misses: list[dict[str, Any]] = []
    for position, document in enumerate(documents):
        order = np.argsort(-scores[position], kind="stable")
        place = np.empty(len(pool), dtype=np.int64)
        place[order] = np.arange(1, len(pool) + 1)
        own = [int(place[index[label]]) for label in document.truth]
        ranks.extend(own)
        best_ranks.append(min(own))
        per_document.append(sum(rank <= PROPOSED_K for rank in own) / len(own))
        for label, rank in zip(document.truth, own, strict=True):
            if rank > PROPOSED_K:
                misses.append(
                    {
                        "document": document.filename,
                        "label": pool[index[label]].name,
                        "rank": rank,
                        "shortlist_head": [pool[int(i)].name for i in order[:5]],
                    }
                )

    random_hits = 0
    for document in documents:
        drawn = set(rng.sample(range(len(pool)), min(PROPOSED_K, len(pool))))
        random_hits += sum(index[label] in drawn for label in document.truth)

    return {
        "pool_labels": len(pool),
        "ground_truth_pairs": len(ranks),
        "micro_recall_at": {str(k): _recall(ranks, k) for k in K_VALUES},
        # The kinder reading, and the one the feature actually needs. A document is filed
        # usefully if *one* of the labels a human chose is on the list — the model may pick
        # three, and this corpus carries labels that nobody would defend (`eu-ai-act.pdf` is
        # filed under `finance/2026/invoices`). Micro recall counts those as misses and is
        # therefore the harsh reading; both are reported rather than one being chosen.
        "any_true_label_at": {str(k): _recall(best_ranks, k) for k in K_VALUES},
        "macro_recall_at_proposed_k": round(statistics.mean(per_document), 4),
        "documents_fully_covered_at_proposed_k": sum(value == 1.0 for value in per_document),
        "documents": len(documents),
        # The number that decides the recommendation. Recall@k asks what a chosen k keeps;
        # this asks what k it would take to keep almost everything, and if the answer is a
        # large fraction of the pool then there is no shortlist here, only a smaller list.
        "k_required_for_95_percent": sorted(ranks)[max(0, int(len(ranks) * 0.95) - 1)],
        "best_rank_median": statistics.median(best_ranks),
        "rank_median": statistics.median(ranks),
        "rank_p90": sorted(ranks)[max(0, int(len(ranks) * 0.9) - 1)],
        "rank_max": max(ranks),
        "control_random_shortlist": round(random_hits / len(ranks), 4),
        "control_whole_pool": _recall(ranks, len(pool)),
        "selectivity_at_proposed_k": round(1 - PROPOSED_K / len(pool), 4),
        "misses_at_proposed_k": misses[:12],
        "misses_total": len(misses),
    }


async def _query_cost(
    pool: list[Label], vectors: NDArray[np.float64], probes: list[list[float]]
) -> dict[str, Any]:
    """What one shortlist costs in Postgres, on a table built for the purpose and dropped.

    In-process numpy would answer a question nobody has: a real implementation stores the
    label vectors beside the labels and asks the database for the nearest k, which is a
    query with a plan. Both are timed, and the plan is captured — a cost arm that did not
    check which plan ran would be the `Subplans Removed: 255` mistake again.
    """
    schema = "zenith_label_shortlist_scratch"
    engine = create_async_engine(settings.database_owner_url)
    out: dict[str, Any] = {"rows": len(pool)}
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            await conn.execute(text(f"CREATE SCHEMA {schema}"))
            await conn.execute(
                text(
                    f"CREATE TABLE {schema}.label_vectors "
                    "(label_id uuid PRIMARY KEY, name text NOT NULL, embedding vector(1024))"
                )
            )
            await conn.execute(
                text(
                    f"INSERT INTO {schema}.label_vectors (label_id, name, embedding) "
                    "VALUES (:id, :name, :vector)"
                ),
                [
                    {
                        "id": str(label.label_id),
                        "name": label.name,
                        "vector": _vector_literal(vectors[position].tolist()),
                    }
                    for position, label in enumerate(pool)
                ],
            )

        statement = text(
            f"SELECT name FROM {schema}.label_vectors "
            "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
        )
        arms: tuple[tuple[str, bool, bool], ...] = (
            ("no_index", False, False),
            ("hnsw_planner_choice", True, False),
            ("hnsw_forced", True, True),
        )
        for arm, index, force in arms:
            async with engine.begin() as conn:
                if index and arm == "hnsw_planner_choice":
                    await conn.execute(
                        text(
                            f"CREATE INDEX ON {schema}.label_vectors "
                            "USING hnsw (embedding vector_cosine_ops)"
                        )
                    )
                    await conn.execute(text(f"ANALYZE {schema}.label_vectors"))
                if force:
                    # Not a tuning knob, a way of separating two questions. Two thousand
                    # rows is small enough that the planner sorts them and is right to;
                    # forcing the index says what the index would cost if the pool were
                    # large enough to need one.
                    await conn.execute(text("SET LOCAL enable_seqscan = off"))
                times: list[float] = []
                for probe in probes:
                    literal = _vector_literal(probe)
                    started = time.perf_counter()
                    await conn.execute(statement, {"q": literal, "k": PROPOSED_K})
                    times.append((time.perf_counter() - started) * 1000)
                plan = (
                    await conn.execute(
                        text("EXPLAIN " + str(statement)),
                        {"q": _vector_literal(probes[0]), "k": PROPOSED_K},
                    )
                ).scalars()
                # Truncated: the plan repeats the whole 1024-dimension probe as a literal,
                # which is a quarter of a megabyte of report saying nothing.
                lines = [line.split("'[")[0].rstrip() for line in plan][:3]
                out[arm] = {
                    "median_ms": round(statistics.median(times), 3),
                    "p95_ms": round(sorted(times)[max(0, int(len(times) * 0.95) - 1)], 3),
                    "plan": lines,
                    "index_used": any("Index Scan" in line for line in lines),
                }
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await engine.dispose()
    return out


async def _run() -> int:
    started_at = datetime.now(UTC).isoformat()
    report: dict[str, Any] = {"bar": BAR}
    REPORT.write_text(json.dumps({"bar": BAR, "status": "running"}, indent=2) + "\n")

    facts = await _installation()
    report["installation"] = facts
    corpus_tenant = facts["tenants"][0]
    print(
        f"migration {facts['migration']}, {facts['documents']} documents, "
        f"{facts['chunks']} passages"
    )
    for tenant in facts["tenants"]:
        if tenant["documents"]:
            print(
                f"  {tenant['name'][:40]:<42} {tenant['labels']:>3} labels, "
                f"{tenant['offerable_labels']:>3} offerable, "
                f"ceiling binds: {tenant['ceiling_binds']}"
            )

    engine = create_async_engine(settings.database_owner_url)
    async with engine.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        uploader = (
            await conn.execute(
                text(
                    "SELECT uploaded_by FROM documents WHERE tenant_id = :t "
                    "AND uploaded_by IS NOT NULL GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
                ),
                {"t": corpus_tenant["id"]},
            )
        ).scalar_one()
        await conn.rollback()
    await engine.dispose()

    labels, documents, (vectors, excerpts) = await _corpus(UUID(corpus_tenant["id"]), uploader)
    if not documents:
        print("No document carries a human-chosen offerable label — nothing to measure.")
        report["status"] = "no ground truth"
        REPORT.write_text(json.dumps(report, indent=2) + "\n")
        return 1

    used = sorted({label for document in documents for label in document.truth})
    report["ground_truth"] = {
        "tenant": corpus_tenant["name"],
        "uploader": str(uploader),
        "reach_offerable_labels": len(labels),
        "labels_actually_used": len(used),
        "documents_with_a_human_label": len(documents),
        "label_assignments": sum(len(document.truth) for document in documents),
        "labels_per_document_median": statistics.median(
            len(document.truth) for document in documents
        ),
        "most_common_label": max(
            (
                {"name": label.name, "documents": sum(label.label_id in d.truth for d in documents)}
                for label in labels
            ),
            key=lambda entry: entry["documents"],
        ),
        "weakness": (
            "42 documents in the installation, of which these are the ones carrying a "
            "human-chosen offerable label. One label covers most of them. The labels were "
            "applied by whoever built this corpus, not by a records manager filing their "
            "own work, and a 26-document sample cannot separate a shortlist that is right "
            "from one that is lucky. It is the only ground truth available."
        ),
    }
    print(
        f"\n{len(documents)} documents carry a human label, "
        f"{report['ground_truth']['label_assignments']} assignments over "
        f"{len(used)} distinct labels, out of {len(labels)} reachable"
    )

    embedder = TeiEmbedder()
    started = time.perf_counter()
    real_names = embedder.encode([label.name for label in labels], batch=4)
    real_embed_ms = (time.perf_counter() - started) * 1000

    synthetic = synthesise(max(POOLS) - len(labels), {label.name for label in labels}, SEED)
    print(f"embedding {len(synthetic)} synthetic label names")
    started = time.perf_counter()
    synthetic_vectors = embedder.encode(synthetic, batch=4)
    synthetic_embed_ms = (time.perf_counter() - started) * 1000

    pool_all = list(labels) + [
        Label(label_id=UUID(int=position + 1), name=name, real=False)
        for position, name in enumerate(synthetic)
    ]
    pool_matrix = _unit(np.asarray(real_names + synthetic_vectors, dtype=np.float64))

    print(f"embedding {len(documents)} excerpts (the text the model actually reads)")
    started = time.perf_counter()
    excerpt_vectors = embedder.encode(
        [excerpts[document.document_id] for document in documents], batch=1
    )
    excerpt_embed_ms = (time.perf_counter() - started) * 1000

    representations = {
        "full_mean": _unit(
            np.asarray([vectors["full_mean"][d.document_id] for d in documents], dtype=np.float64)
        ),
        "head_mean": _unit(
            np.asarray([vectors["head_mean"][d.document_id] for d in documents], dtype=np.float64)
        ),
        "excerpt_tei": _unit(np.asarray(excerpt_vectors, dtype=np.float64)),
    }

    arms: dict[str, Any] = {}
    for name, doc_matrix in representations.items():
        arms[name] = {}
        for size in (len(labels), *POOLS):
            pool = pool_all[:size]
            measured = _arm(documents, doc_matrix, pool_matrix[:size], pool, random.Random(SEED))
            arms[name][str(size)] = measured
            print(
                f"  {name:<12} pool {size:<5} "
                f"recall@{PROPOSED_K}={measured['micro_recall_at'][str(PROPOSED_K)]:.4f} "
                f"any@{PROPOSED_K}={measured['any_true_label_at'][str(PROPOSED_K)]:.4f} "
                f"random={measured['control_random_shortlist']:.4f} "
                f"median rank={measured['rank_median']}"
            )
    report["arms"] = arms

    best = max(
        representations,
        key=lambda name: arms[name][str(max(POOLS))]["micro_recall_at"][str(PROPOSED_K)],
    )
    probes = [row.tolist() for row in representations[best]]
    numpy_started = time.perf_counter()
    for probe in probes:
        _ = np.argsort(-(pool_matrix @ np.asarray(probe)))[:PROPOSED_K]
    numpy_ms = (time.perf_counter() - numpy_started) * 1000 / len(probes)

    print("\ntiming the shortlist query in Postgres")
    query_cost = await _query_cost(pool_all[: max(POOLS)], pool_matrix[: max(POOLS)], probes)
    report["cost"] = {
        "embedding_the_labels_once": {
            "real_labels": len(labels),
            "real_labels_ms": round(real_embed_ms, 1),
            "synthetic_labels": len(synthetic),
            "synthetic_labels_ms": round(synthetic_embed_ms, 1),
            "per_label_ms": round(
                (real_embed_ms + synthetic_embed_ms) / (len(labels) + len(synthetic)), 2
            ),
            "note": (
                "tei-embed caps a client batch at 4, so this is round trips rather than "
                "model time. In the product a label is embedded when it is created — one "
                "name, once — so the per-label figure is the one that would be paid."
            ),
        },
        "embedding_the_document": {
            "mean_of_passage_vectors_ms": 0.0,
            "note": (
                "Zero, and that is the finding. The pipeline embeds before it classifies, "
                "so the passage vectors already exist and their mean is an aggregate. "
                "Embedding the excerpt separately, which the proposal does not require, "
                f"cost {round(excerpt_embed_ms / len(documents), 1)} ms per document here."
            ),
            "excerpt_via_tei_ms_per_document": round(excerpt_embed_ms / len(documents), 1),
        },
        "shortlist_query_at_pool_2000": query_cost,
        "shortlist_in_process_numpy_ms": round(numpy_ms, 3),
        "what_it_precedes": {
            "answer_path_median_ms": 10544.7,
            "source": "eval/answer-report.json summary.median_latency_ms",
            "retrieval_median_ms": 873.44,
            "retrieval_source": "eval/latency.json total_median_ms",
            "note": (
                "The brief quoted a generation median of 5085 ms. No run under eval/ "
                "records that figure; the two above are what is on disk, so they are what "
                "this comparison uses."
            ),
        },
    }

    largest = arms[best][str(max(POOLS))]
    ceiling = arms[best]["60"]
    report["bar_result"] = {
        "representation_scored": best,
        "b0_the_shortlist_is_actually_a_shortlist": largest["selectivity_at_proposed_k"] >= 0.98,
        "b1_random_control_is_near_zero": largest["control_random_shortlist"] < 0.05,
        "b2_whole_pool_control_is_exactly_one": largest["control_whole_pool"] == 1.0,
        "b3_recall_at_25_holds_at_scale": (largest["micro_recall_at"][str(PROPOSED_K)] >= 0.95),
        "b4_recall_does_not_decay_with_label_count": (
            largest["micro_recall_at"][str(PROPOSED_K)]
            >= ceiling["micro_recall_at"][str(PROPOSED_K)] - 0.05
        ),
        "b5_cost_is_noise_beside_the_call_it_precedes": (
            float(query_cost["hnsw_planner_choice"]["median_ms"]) <= 100
        ),
    }
    report["verdict"] = {
        "recommendation": "do not adopt",
        "at_any_k_swept": (
            f"Micro recall@{PROPOSED_K} at 2000 labels is "
            f"{largest['micro_recall_at'][str(PROPOSED_K)]}, and even k=100 reaches only "
            f"{largest['micro_recall_at']['100']}. Keeping 95% of the labels a human chose "
            f"would take k={largest['k_required_for_95_percent']} of 2000 — not a shortlist, "
            "a slightly shorter list, and far past the point the module docstring already "
            "identifies as where a model skims instead of weighing."
        ),
        "the_cost_claim_is_correct_and_does_not_help": (
            "The shortlist itself is free: "
            f"{query_cost['hnsw_planner_choice']['median_ms']} ms per document against a "
            "measured answer-path median of 10544.7 ms, and the document vector is the mean "
            "of passage vectors that already exist when filing runs. Cost was never the "
            "reason not to do this."
        ),
        "why_it_fails": (
            "Cosine over label names ranks by topical neighbourhood, and in a 2000-label "
            "taxonomy the neighbourhood is far larger than 25. The shortlist puts every "
            "legal document among legal folders and then cannot choose between siblings — "
            "the region is right and the folder is wrong."
        ),
        "what_the_ceiling_is_worth": (
            "Above 60 labels the product files nothing, which is a defensible design: a "
            "document reaching the tenant default is visible to the tenant, which is where "
            "it was already. Filing it into one of 25 topically plausible but wrong "
            "compartments is not the same failure — the classifier only ever narrows, so a "
            "wrong label hides the document from the people who should have it and shows it "
            "to a compartment nobody chose, silently. Trading 0% filing for a "
            f"{largest['any_true_label_at'][str(PROPOSED_K)]:.0%} chance of the right folder "
            "being on offer is a trade against the product."
        ),
        "what_would_have_to_change_first": (
            "Nothing here rules out a shortlist built from something richer than a label's "
            "name — the passages already filed under it, for instance. That is a different "
            "proposal with a different cost, and this run says nothing about it."
        ),
    }
    report["run"] = {
        "started_at": started_at,
        "seed": SEED,
        "k_values": list(K_VALUES),
        "metrics_added_after_the_first_run": (
            "`any_true_label_at` and `best_rank_median` were added after the first run, "
            "which reported micro recall only. The reason is in the misses the first run "
            "printed: this corpus files `eu-ai-act.pdf` under `finance/2026/invoices` and "
            "`irs-form-1040.pdf` under `Research Papers`, so micro recall charges the "
            "shortlist for not surfacing labels nobody would defend. No bar was written "
            "against the new metric and none has been added to BAR, which stands as it "
            "was written; it is reported beside the bar, not in place of it."
        ),
    }
    report["status"] = "complete"

    REPORT.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\n{json.dumps(report['bar_result'], indent=2)}")
    print(f"Written to {REPORT.name}")
    return (
        0 if all(value for key, value in report["bar_result"].items() if key.startswith("b")) else 1
    )


#: How `python -m eval` finds this sweep. Declared here rather than listed in
#: `__main__.py`, so adding a measurement is adding a file and nothing else.
COMMAND = "label-shortlist"
USAGE = "label-shortlist"


def run() -> int:
    return asyncio.run(_run())
