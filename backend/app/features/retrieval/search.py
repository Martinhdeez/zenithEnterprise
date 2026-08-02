"""The two halves of hybrid search, and the fusion that combines them.

Neither query filters by tenant or by label. Both run inside `tenant_session`, and the
policies on `chunks` and `chunk_embeddings` do it — the same argument as everywhere else in
this system: a filter in application code is correct until someone writes the query that
forgets it, and that query looks exactly like the one that does not.

**One join here is a security control rather than a convenience.** The policy on
`chunk_embeddings` is tenant-scoped only (`tenant_id = zenith_current_tenant()`), because it
sits on the hot path of vector search and a per-row `EXISTS` against `chunks` would be paid
on every query. Label filtering therefore arrives through the join to `chunks`, whose policy
checks `label_ids`. Dropping that join to "simplify" the query would return passages from
documents the caller cannot open.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.retrieval.lexical import CONFIGURATION, to_tsquery

CANDIDATES = 50

# Reciprocal Rank Fusion. `k` damps the influence of the very top positions, so a chunk
# ranked first by one half and absent from the other does not automatically beat a chunk
# ranked third by both. 60 is the value from the original paper and the one M0 measured at.
RRF_K = 60


@dataclass(frozen=True, slots=True)
class Hit:
    chunk_id: UUID
    document_id: UUID
    filename: str
    page_num: int
    text: str
    bboxes: list[dict[str, float]]
    # Positions rather than scores. `ts_rank_cd` and cosine distance live on different,
    # corpus-dependent scales; keeping both raw makes the response self-describing without
    # implying they are comparable.
    lexical_rank: int | None
    dense_rank: int | None
    score: float


async def lexical(session: AsyncSession, question: str, limit: int = CANDIDATES) -> list[UUID]:
    """Exact terms: identifiers, product codes, proper nouns, acronyms.

    `ts_rank_cd` rather than `ts_rank` because it accounts for term proximity. For an
    identifier query the match is exact either way; for prose, proximity is what separates a
    passage *about* the subject from one that merely mentions it.
    """
    query = await to_tsquery(session, question)
    if not query:
        return []

    rows = await session.execute(
        text(
            "SELECT c.id FROM chunks c, to_tsquery(:config, :query) q "
            "WHERE c.tsv @@ q "
            "ORDER BY ts_rank_cd(c.tsv, q) DESC, c.id LIMIT :limit"
        ),
        {"config": CONFIGURATION, "query": query, "limit": limit},
    )
    return [row.id for row in rows]


async def dense(
    session: AsyncSession,
    embedding: list[float],
    model: str,
    version: str,
    limit: int = CANDIDATES,
    ef_search: int | None = None,
) -> list[UUID]:
    """Meaning: intent, synonyms, paraphrase — everything the lexical half cannot reach.

    Restricted to one embedding space. `embedding_spaces` exists so several can coexist
    during a reindex (RNF-08), and vectors from two models are not comparable — a query that
    forgot this filter would rank across incompatible spaces and return confident nonsense.
    """
    if ef_search:
        # A per-session knob, and a speed/recall trade, which is why the value comes from
        # the hardware profile. Left at pgvector's default, recall from the index itself
        # caps below what the data supports.
        await session.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))

    rows = await session.execute(
        text(
            "SELECT c.id FROM chunk_embeddings e "
            # Not decoration: `chunk_embeddings` is filtered by tenant only, so this join is
            # where label isolation is enforced for the dense half.
            "JOIN chunks c ON c.id = e.chunk_id "
            "WHERE e.embedding_model = :model AND e.embedding_version = :version "
            "ORDER BY e.embedding <=> CAST(:embedding AS vector) LIMIT :limit"
        ),
        {
            "model": model,
            "version": version,
            "embedding": str(embedding),
            "limit": limit,
        },
    )
    return [row.id for row in rows]


def fuse(lexical_ids: list[UUID], dense_ids: list[UUID], limit: int) -> list[tuple[UUID, float]]:
    """Reciprocal Rank Fusion — positions only, never scores.

    BM25-style relevance and cosine similarity are not comparable: they live on different
    scales that move with the corpus. Normalising them is fragile and needs recalibrating
    whenever the data changes. RRF ignores the magnitudes entirely, which is what makes it
    robust enough to have one parameter.
    """
    scores: dict[UUID, float] = {}
    for ranking in (lexical_ids, dense_ids):
        for position, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1 / (RRF_K + position)

    ordered = sorted(scores.items(), key=lambda item: (-item[1], str(item[0])))
    return ordered[:limit]


async def hydrate(
    session: AsyncSession,
    ranked: list[tuple[UUID, float]],
    lexical_ids: list[UUID],
    dense_ids: list[UUID],
) -> list[Hit]:
    """Fetch what a citation needs, for the fused set only.

    Deliberately after fusion rather than in the two candidate queries: the halves return
    100 ids between them and only a handful survive, so joining `documents` and carrying
    chunk text through both would be paid for rows nobody sees.

    The bounding boxes and page number are here because F7's viewer needs them, and the two
    ranks because six months from now someone will ask why a particular answer was wrong.
    That is the same reasoning that put four score columns in `query_citations`.
    """
    if not ranked:
        return []

    order = {chunk_id: position for position, (chunk_id, _) in enumerate(ranked)}
    lexical_positions = {chunk_id: index + 1 for index, chunk_id in enumerate(lexical_ids)}
    dense_positions = {chunk_id: index + 1 for index, chunk_id in enumerate(dense_ids)}

    rows = await session.execute(
        text(
            "SELECT c.id, c.document_id, d.filename, c.page_num, c.text, c.bboxes "
            "FROM chunks c JOIN documents d ON d.id = c.document_id "
            "WHERE c.id = ANY(:ids)"
        ),
        {"ids": [chunk_id for chunk_id, _ in ranked]},
    )

    hits = [
        Hit(
            chunk_id=row.id,
            document_id=row.document_id,
            filename=row.filename,
            page_num=row.page_num,
            text=row.text,
            bboxes=list(row.bboxes or []),
            lexical_rank=lexical_positions.get(row.id),
            dense_rank=dense_positions.get(row.id),
            score=dict(ranked)[row.id],
        )
        for row in rows
    ]
    # Re-sorted in Python: `= ANY(...)` does not preserve the order of the array, and the
    # fused order is the entire product of this feature.
    return sorted(hits, key=lambda hit: order[hit.chunk_id])
