# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""The M0 baseline, re-measured through the code that ships.

`eval/pipeline.py` was the tracer bullet: its own table, its own chunker, its own fusion,
no RLS. It produced 75% Recall@8, and that number described a prototype nobody would ever
install. This module runs the same corpus and the same 36 questions through
**`SearchService`** — production chunking, production routing, the real `chunks` and
`chunk_embeddings` tables, real policies, real HNSW index.

If the number comes back at or above 75%, the baseline transfers and F6 is done. If it
comes back lower, something in the production path costs recall and we would rather find
out here than from a customer.

**Embedding happens through PyTorch, not TEI.** TEI publishes `linux/amd64` only, so on a
development Mac it would run emulated. The weights are identical, so the same text produces
the same vector and the same ranking — which is exactly the split M0 established: quality
on the laptop, resources on the machine a customer would use.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text

from app.features.retrieval.reranker import Scored

REPORT = Path(__file__).resolve().parent / "production-recall.json"

# Embedding the whole corpus takes tens of minutes on this laptop. The cache is keyed on the
# chunk texts themselves, so it survives a failed run and invalidates the moment chunking
# changes — which is the only thing that should invalidate it.
VECTOR_CACHE = Path(__file__).resolve().parent / ".vector-cache.npy"


@dataclass
class Outcome:
    question_id: str
    type: str
    hit_at_8: bool = False
    hit_at_50: bool = False
    document_hit_at_8: bool = False
    first_rank: int | None = None
    lexical_only: bool = False
    dense_only: bool = False
    elapsed_ms: float = 0.0


@dataclass
class Corpus:
    tenant_id: UUID
    label_id: UUID
    user_id: UUID
    documents: dict[str, UUID] = field(default_factory=dict)


class LocalQueryEmbedder:
    """`embed_query` through PyTorch, for the harness only.

    The one seam between the measurement and production search. Everything else in the
    query path — tokenisation, both candidate queries, fusion, hydration — is the shipped
    code.
    """

    def __init__(self) -> None:
        from eval.embedder import LocalEmbedder

        self.model = LocalEmbedder()

    async def embed_query(self, question: str) -> list[float]:
        return self.model.encode([question], batch=1)[0]


class LocalReranker:
    """The cross-encoder through PyTorch, for the harness only.

    Same reason as `LocalQueryEmbedder`: TEI publishes `linux/amd64`, and `docker compose up
    tei-rerank` on Apple Silicon fails outright with "no matching manifest for
    linux/arm64/v8". The weights are identical, so the pair scores and therefore the final
    order are identical — which is exactly the split M0 established.

    It matches `TeiReranker.rank`'s shape rather than inheriting it, so the production class
    keeps no knowledge that a laboratory variant exists.
    """

    def __init__(self) -> None:
        from sentence_transformers import CrossEncoder

        from app.features.retrieval.reranker import MODEL

        self.model = CrossEncoder(MODEL)

    async def rank(self, question: str, passages: list[str]) -> list["Scored"]:
        scores = self.model.predict([(question, passage) for passage in passages])
        ranked = [Scored(index=index, score=float(score)) for index, score in enumerate(scores)]
        return sorted(ranked, key=lambda item: (-item.score, item.index))


async def provision() -> Corpus:
    """A tenant to hold the corpus, seeded the way the install CLI would."""
    from app.core.database import owner_session
    from app.features.tenancy.service import TenantService

    tenant = await TenantService().create(f"M0 baseline {uuid4()}")
    async with owner_session() as session:
        label_id = await session.scalar(
            text("SELECT id FROM access_labels WHERE tenant_id = :t AND is_default"),
            {"t": tenant.id},
        )
        user_id = await session.scalar(
            text("SELECT id FROM users WHERE tenant_id = :t LIMIT 1"), {"t": tenant.id}
        )
        if user_id is None:
            user_id = uuid4()
    return Corpus(tenant_id=tenant.id, label_id=UUID(str(label_id)), user_id=UUID(str(user_id)))


async def ingest(corpus: Corpus) -> int:
    """Parse and chunk with production code, then write the production tables.

    The parser, the router and the chunker are the shipped ones. The *writing* is direct
    rather than through `IngestionPipeline`, because the pipeline embeds via TEI and this
    machine embeds via PyTorch — swapping that one call is the whole difference, and going
    through the pipeline would mean faking an HTTP service to avoid faking a function.
    """
    from app.core.database import owner_session
    from app.features.embeddings.client import DIMENSION, MODEL, VERSION
    from app.features.ingestion.chunking.chunker import chunk_page
    from app.features.ingestion.parsers.pdfplumber_parser import PdfPlumberParser
    from app.features.ingestion.routing import Route, decide
    from eval.corpus import load_manifest

    documents = [document for document in load_manifest() if document.path.exists()]
    all_chunks: list[tuple[str, int, str]] = []

    for document in documents:
        pages = PdfPlumberParser().parse(document.path)
        produced = 0
        for page in pages:
            # OCR is unavailable here, so an unreadable page is skipped rather than
            # pretended into the index — the same refusal the pipeline makes.
            if decide(page, ocr_available=False).route is Route.UNREADABLE:
                continue
            for chunk in chunk_page(page):
                all_chunks.append((document.id, chunk.page_num, chunk.text))
                produced += 1
        print(f"{document.id:<28} {len(pages):>5}p {produced:>5} chunks", flush=True)

    texts = [chunk[2] for chunk in all_chunks]
    vectors = _embed_cached(texts, MODEL)

    async with owner_session() as session:
        await session.execute(
            text(
                "INSERT INTO embedding_spaces (model, version, dimension, status) "
                "VALUES (:m, :v, :d, 'active') ON CONFLICT DO NOTHING"
            ),
            {"m": MODEL, "v": VERSION, "d": DIMENSION},
        )
        for document in documents:
            document_id = await session.scalar(
                text(
                    "INSERT INTO documents (tenant_id, filename, sha256, size_bytes, status, "
                    "page_count) VALUES (:t, :name, :sha, :size, 'ready', 0) RETURNING id"
                ),
                {
                    "t": corpus.tenant_id,
                    # The corpus id goes in `filename` so a hit can be attributed back to the
                    # question set without a second lookup table.
                    "name": document.id,
                    "sha": (document.sha256 or str(uuid4()))[:64],
                    "size": document.bytes or 0,
                },
            )
            await session.execute(
                text("INSERT INTO document_labels (document_id, label_id) VALUES (:d, :l)"),
                {"d": document_id, "l": corpus.label_id},
            )
            corpus.documents[document.id] = UUID(str(document_id))

        for (document_key, page, body), vector in zip(all_chunks, vectors, strict=True):
            chunk_id = await session.scalar(
                text(
                    "INSERT INTO chunks (document_id, tenant_id, page_num, char_start, "
                    "char_end, text, bboxes) VALUES (:d, :t, :p, 0, :e, :body, '[]') "
                    "RETURNING id"
                ),
                {
                    "d": corpus.documents[document_key],
                    "t": corpus.tenant_id,
                    "p": page,
                    "e": len(body),
                    "body": body,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO chunk_embeddings (chunk_id, tenant_id, embedding_model, "
                    "embedding_version, embedding) VALUES (:c, :t, :m, :v, "
                    "CAST(:embedding AS vector))"
                ),
                {
                    "c": chunk_id,
                    "t": corpus.tenant_id,
                    "m": MODEL,
                    "v": VERSION,
                    "embedding": str(list(vector)),
                },
            )

    return len(all_chunks)


def _embed_cached(texts: list[str], model: str) -> list[list[float]]:
    """Embed, reusing the previous run's vectors when the chunk texts are unchanged.

    Not an optimisation for its own sake: without it, any failure after the embedding step
    costs another forty minutes, and a measurement nobody can afford to repeat is one that
    stops being repeated.
    """
    import hashlib

    import numpy

    from eval.embedder import LocalEmbedder

    fingerprint = hashlib.sha256(("\x00".join(texts) + model).encode()).hexdigest()
    marker = VECTOR_CACHE.with_suffix(".key")

    if VECTOR_CACHE.exists() and marker.exists() and marker.read_text() == fingerprint:
        print(f"reusing cached vectors for {len(texts)} chunks", flush=True)
        # `float()` per element, not `list(row)`: a numpy float32 renders as
        # `np.float32(0.0113)` under `str()`, and pgvector parses the text form.
        return [[float(value) for value in row] for row in numpy.load(VECTOR_CACHE)]

    print(f"\n{len(texts)} chunks — embedding with {model}", flush=True)
    vectors = [[float(value) for value in row] for row in LocalEmbedder().encode(texts, batch=16)]
    numpy.save(VECTOR_CACHE, numpy.array(vectors, dtype=numpy.float32))
    marker.write_text(fingerprint)
    return vectors


async def measure(corpus: Corpus) -> list[Outcome]:
    """Every question through `SearchService`, scored against the recorded pages."""
    from app.features.auth.permissions import CATALOGUE
    from app.features.auth.service import AccessProfile
    from app.features.retrieval.service import SearchService
    from app.features.tenancy.context import TenantContext
    from eval.questions import load_questions

    profile = AccessProfile(
        user_id=corpus.user_id,
        context=TenantContext.for_tenant(corpus.tenant_id, [corpus.label_id]),
        permissions=frozenset(CATALOGUE),
    )
    import os

    hardware = None
    reranker = None
    if os.environ.get("ZENITH_EVAL_RERANK"):
        from app.core.hardware import PROFILES

        hardware = PROFILES["cpu"]
        reranker = LocalReranker()
        print("reranking with the local cross-encoder", flush=True)

    service = SearchService(
        profile,
        embedder=LocalQueryEmbedder(),  # type: ignore[arg-type]
        hardware=hardware,
        reranker=reranker,  # type: ignore[arg-type]
    )
    by_uuid = {value: key for key, value in corpus.documents.items()}

    outcomes: list[Outcome] = []
    for question in load_questions():
        expected = {(source.document, page) for source in question.sources for page in source.pages}
        started = time.perf_counter()
        top = await service.search(question.question, limit=50)
        elapsed = (time.perf_counter() - started) * 1000

        outcome = Outcome(question_id=question.id, type=question.type, elapsed_ms=elapsed)
        if question.type == "unanswerable":
            outcomes.append(outcome)
            continue

        located = [(by_uuid.get(hit.document_id, "?"), hit.page_num) for hit in top.hits]
        outcome.hit_at_8 = any(place in expected for place in located[:8])
        outcome.hit_at_50 = any(place in expected for place in located)
        outcome.document_hit_at_8 = any(
            document in {name for name, _ in expected} for document, _ in located[:8]
        )
        for rank, place in enumerate(located, start=1):
            if place in expected:
                outcome.first_rank = rank
                break

        hits_by_half = [
            (hit.lexical_rank, hit.dense_rank)
            for hit, place in zip(top.hits, located, strict=True)
            if place in expected
        ]
        outcome.lexical_only = any(dense is None for _, dense in hits_by_half)
        outcome.dense_only = any(lexical is None for lexical, _ in hits_by_half)

        outcomes.append(outcome)
        print(
            f"{question.id:<26} {question.type:<15} "
            f"@8={'hit ' if outcome.hit_at_8 else 'MISS'} "
            f"@50={'hit ' if outcome.hit_at_50 else 'MISS'} "
            f"rank={outcome.first_rank or '-':<4} {elapsed:>6.0f}ms",
            flush=True,
        )
    return outcomes


def report(outcomes: list[Outcome], chunks: int) -> dict[str, object]:
    from eval.questions import HEADLINE_TYPES

    headline = [item for item in outcomes if item.type in HEADLINE_TYPES]
    scored = [item for item in outcomes if item.type != "unanswerable"]

    def ratio(items: list[Outcome], attribute: str) -> float:
        return sum(getattr(item, attribute) for item in items) / len(items) if items else 0.0

    return {
        "chunks": chunks,
        "questions": len(outcomes),
        "headline_questions": len(headline),
        "recall_at_8": round(ratio(headline, "hit_at_8"), 4),
        "recall_at_50": round(ratio(headline, "hit_at_50"), 4),
        "document_recall_at_8": round(ratio(headline, "document_hit_at_8"), 4),
        "recall_at_8_all_scored": round(ratio(scored, "hit_at_8"), 4),
        "lexical_only_hits": sum(item.lexical_only for item in scored),
        "dense_only_hits": sum(item.dense_only for item in scored),
        "median_latency_ms": round(
            sorted(item.elapsed_ms for item in outcomes)[len(outcomes) // 2], 1
        ),
        "by_type": {
            kind: round(
                ratio([item for item in scored if item.type == kind], "hit_at_8"),
                4,
            )
            for kind in sorted({item.type for item in scored})
        },
    }


async def run(skip_ingest: bool = False) -> None:
    from app.core.database import owner_session

    corpus = await provision()
    chunks = 0
    if not skip_ingest:
        chunks = await ingest(corpus)
    else:
        async with owner_session() as session:
            chunks = await session.scalar(text("SELECT count(*) FROM chunks")) or 0

    outcomes = await measure(corpus)
    summary = report(outcomes, chunks)
    print("\n" + json.dumps(summary, indent=2))
    REPORT.write_text(
        json.dumps({"summary": summary, "outcomes": [vars(o) for o in outcomes]}, indent=2)
    )


if __name__ == "__main__":
    asyncio.run(run())
