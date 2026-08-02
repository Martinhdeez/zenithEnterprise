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


def candidates(lexical_ids: list[UUID], dense_ids: list[UUID]) -> list[tuple[UUID, float]]:
    """Everything either half proposed, in fused order.

    The union rather than the fused top-N, because fusion was doing two jobs and is bad at
    one of them. Choosing candidates and ordering results are different problems: a passage
    only the lexical half found is *exactly* the case worth reranking, and it is exactly the
    case RRF discards.

    Measured: identifier questions scored 0% at rank 8, and two of six were not in the fused
    top-50 at all — found by the lexical half, ranked out of existence by agreement.
    """
    return fuse(lexical_ids, dense_ids, limit=len(lexical_ids) + len(dense_ids))


def fuse(lexical_ids: list[UUID], dense_ids: list[UUID], limit: int) -> list[tuple[UUID, float]]:
    """Reciprocal Rank Fusion — positions only, never scores.

    BM25-style relevance and cosine similarity are not comparable: they live on different
    scales that move with the corpus. Normalising them is fragile and needs recalibrating
    whenever the data changes. RRF ignores the magnitudes entirely, which is what makes it
    robust enough to have one parameter.

    **With one correction, measured rather than reasoned.** RRF rewards agreement, and an
    exact identifier match is by nature a passage only one half can find:

        ranked 1st lexically, absent from dense →  1/61  = 0.0164
        ranked 5th by both                      →  2/65  = 0.0308   ← wins

    So a passage containing the exact string someone searched for loses to two mediocre
    agreements. `_promote_leaders` guarantees the top hit of each half a place in the
    result. Not a weight and not a tuning parameter — a floor, and the smallest change that
    fixes the case without reintroducing the scale problem RRF was chosen to avoid.
    """
    scores: dict[UUID, float] = {}
    for ranking in (lexical_ids, dense_ids):
        for position, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1 / (RRF_K + position)

    ordered = sorted(scores.items(), key=lambda item: (-item[1], str(item[0])))
    return _promote_leaders(ordered[:limit], lexical_ids, dense_ids, limit)


def _promote_leaders(
    top: list[tuple[UUID, float]],
    lexical_ids: list[UUID],
    dense_ids: list[UUID],
    limit: int,
) -> list[tuple[UUID, float]]:
    """If either half ranked something first, it appears.

    Applied after ordering rather than as a score adjustment, so the fused order is
    untouched for everything else — the promoted entry takes the last place rather than
    displacing the top of the list, because being one half's favourite is evidence, not
    proof.
    """
    if not top or limit < 2:
        return top

    present = {chunk_id for chunk_id, _ in top}
    leaders = [ranking[0] for ranking in (lexical_ids, dense_ids) if ranking]
    missing = [leader for leader in leaders if leader not in present]
    if not missing:
        return top

    kept = top[: max(1, limit - len(missing))]
    floor = kept[-1][1] if kept else 0.0
    return kept + [(leader, floor) for leader in missing]


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
