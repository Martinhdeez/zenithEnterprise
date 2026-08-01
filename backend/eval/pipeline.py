# pyright: reportCallIssue=false, reportArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# psycopg 3.3 types `execute` against `Template`, and sentence-transformers ships partial
# stubs, so strict mode reports every call here as unknown. Suppressed once per file rather
# than on twenty individual lines. Nothing under `app/` relaxes strictness — this is
# laboratory code that never ships, and the M0 plan says so explicitly.

"""The tracer bullet: parse, chunk, embed, index, retrieve.

**Deliberately bad, and every simplification is a variable held constant.** No per-page
parser routing, no contextual prefixes, no bounding boxes, no reranker, no queue, no RLS.
If retrieval works well here, those are refinements. If it works badly, we learn *which
stage* is weak before four more features are built on top of it.

This file is disposable. The corpus, the questions and the measurement are not.
"""

import os
from dataclasses import dataclass

import psycopg
from pgvector.psycopg import register_vector

from eval.corpus import load_manifest
from eval.embedder import DIMENSION, MODEL  # noqa: F401  (re-exported for callers)
from eval.text import extract, normalise

# The owner connection, because this is a laboratory: no tenant context, no policies, no
# ceremony. Nothing here ships.
DSN = os.environ.get("ZENITH_EVAL_DSN", "postgresql://zenith:zenith@localhost:5432/zenith")

# ~1,200 characters with 200 of overlap. Chosen to be unremarkable: the point of M0 is to
# measure the architecture, not to tune chunking. Tuning happens in F5 against this number.
CHUNK_CHARS = 1200
OVERLAP = 200


@dataclass(frozen=True, slots=True)
class Chunk:
    document_id: str
    page: int
    text: str


def chunk_document(document_id: str) -> list[Chunk]:
    """Fixed-size windows over each page, never spanning pages.

    Not spanning pages is the one non-arbitrary decision here: a chunk that crosses a page
    boundary cannot be attributed to a single page, and the question set records pages.
    Mixing the two would make the measurement unable to tell a retrieval miss from a
    bookkeeping error.
    """
    chunks: list[Chunk] = []
    for page_number, raw in enumerate(extract(document_id), start=1):
        text = normalise(raw)
        if len(text) < 40:
            continue
        start = 0
        while start < len(text):
            piece = text[start : start + CHUNK_CHARS]
            if len(piece.strip()) >= 40:
                chunks.append(Chunk(document_id, page_number, piece))
            if start + CHUNK_CHARS >= len(text):
                break
            start += CHUNK_CHARS - OVERLAP
    return chunks


def embed(texts: list[str], batch: int = 16) -> list[list[float]]:
    """Whichever backend this host is configured for — see `eval.embedder`."""
    from eval.embedder import get_embedder

    return get_embedder().encode(texts, batch=batch)


def reset_schema(connection: psycopg.Connection) -> None:
    """A standalone table rather than the production schema.

    `chunks` carries RLS, triggers, a generated tsvector and foreign keys to documents that
    do not exist yet — F4 has not been built. Borrowing it would mean fabricating rows in
    six tables to satisfy constraints that have nothing to do with what is being measured.

    The columns that matter are identical: text, a tsvector for the lexical half, and a
    1024-dimension vector with an HNSW index for the dense half.
    """
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS eval_chunks")
        cursor.execute(f"""
            CREATE TABLE eval_chunks (
                id          bigserial PRIMARY KEY,
                document_id text NOT NULL,
                page        integer NOT NULL,
                text        text NOT NULL,
                tsv         tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
                embedding   vector({DIMENSION})
            )
        """)
        cursor.execute("CREATE INDEX eval_chunks_tsv ON eval_chunks USING gin (tsv)")
    connection.commit()


def index_all(
    connection: psycopg.Connection, chunks: list[Chunk], vectors: list[list[float]]
) -> None:
    with connection.cursor() as cursor:
        with cursor.copy(
            "COPY eval_chunks (document_id, page, text, embedding) FROM STDIN"
        ) as copy:
            for chunk, vector in zip(chunks, vectors, strict=True):
                copy.write_row((chunk.document_id, chunk.page, chunk.text, str(vector)))
        # Built after loading: incremental HNSW insertion is far slower than one bulk build.
        cursor.execute(
            "CREATE INDEX eval_chunks_hnsw ON eval_chunks "
            "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
        )
    connection.commit()


def _to_tsquery(connection: psycopg.Connection, question: str) -> str:
    """OR'd lexemes, tokenised by Postgres itself rather than by a regex here.

    **The first version used `re.findall(r"[A-Za-z0-9\']+", ...)`, and that was a bug that
    invalidated a conclusion.** It splits on exactly the punctuation that makes an
    identifier an identifier: `1545-0074` became `1545` and `0074`, and `119/33` became
    `119`. Meanwhile `to_tsvector` had stored those as the single lexemes `1545`, `-0074`
    and `119/33`. The corpus side preserved them and the query side destroyed them, so the
    lexical half could not match an exact reference even in principle — and M0's first pass
    read that as "the lexical half contributes nothing".

    Running the question through `to_tsvector` guarantees both sides tokenise identically,
    which is the property that was missing rather than a cleverer heuristic.

    Still OR rather than AND: `plainto_tsquery` requires every term, so one word absent from
    a chunk excludes it, and for a natural-language question that excludes nearly everything.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT array_to_string(tsvector_to_array(to_tsvector('english', %s)), ' | ')",
            (question,),
        )
        row = cursor.fetchone()
    return row[0] if row and row[0] else "the"


def search_lexical(connection: psycopg.Connection, question: str, limit: int) -> list[int]:
    query = _to_tsquery(connection, question)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id FROM eval_chunks WHERE tsv @@ to_tsquery('english', %s) "
            "ORDER BY ts_rank_cd(tsv, to_tsquery('english', %s)) DESC LIMIT %s",
            (query, query, limit),
        )
        return [row[0] for row in cursor.fetchall()]


def search_dense(
    connection: psycopg.Connection,
    vector: list[float],
    limit: int,
    documents: list[str] | None = None,
) -> list[int]:
    with connection.cursor() as cursor:
        if documents is None:
            cursor.execute(
                "SELECT id FROM eval_chunks ORDER BY embedding <=> %s::vector LIMIT %s",
                (str(vector), limit),
            )
        else:
            # The label-filtering case: a user who reaches only part of the corpus. This is
            # the shape of query that degrades HNSW, which is what §3.1 measures.
            cursor.execute(
                "SELECT id FROM eval_chunks WHERE document_id = ANY(%s) "
                "ORDER BY embedding <=> %s::vector LIMIT %s",
                (documents, str(vector), limit),
            )
        return [row[0] for row in cursor.fetchall()]


def fuse(rankings: list[list[int]], k: int = 60) -> list[int]:
    """Reciprocal Rank Fusion: score(d) = Σ 1 / (k + rank(d)).

    Positions only, never scores. BM25 and cosine distance live on different,
    corpus-dependent scales, so normalising them is fragile and needs recalibrating
    whenever the data changes. RRF has one parameter and it barely matters.
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda chunk_id: scores[chunk_id], reverse=True)


def chunk_locations(connection: psycopg.Connection, ids: list[int]) -> list[tuple[str, int]]:
    if not ids:
        return []
    with connection.cursor() as cursor:
        cursor.execute("SELECT id, document_id, page FROM eval_chunks WHERE id = ANY(%s)", (ids,))
        found = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
    return [found[chunk_id] for chunk_id in ids if chunk_id in found]


def connect() -> psycopg.Connection:
    connection = psycopg.connect(DSN)
    register_vector(connection)
    return connection


def build() -> None:
    """Parse, chunk, embed and index the whole corpus."""
    documents = [document for document in load_manifest() if document.path.exists()]
    chunks: list[Chunk] = []
    for document in documents:
        produced = chunk_document(document.id)
        chunks.extend(produced)
        print(f"{document.id:<28} {len(produced):>5} chunks")
    print(f"\n{len(chunks)} chunks total — embedding with {MODEL}")

    vectors = embed([chunk.text for chunk in chunks])

    connection = connect()
    reset_schema(connection)
    index_all(connection, chunks, vectors)
    connection.close()
    print("indexed")


if __name__ == "__main__":
    build()
