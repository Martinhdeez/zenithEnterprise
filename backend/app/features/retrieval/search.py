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

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.embeddings.space import NoActiveEmbeddingSpace, Space
from app.features.retrieval.lexical import CONFIGURATION, engine, to_tsquery

CANDIDATES = 50

# pgvector 0.8's answer to the post-filter problem. An HNSW scan walks the graph for the
# nearest `ef` vectors and *then* applies whatever predicate stands over it; every one of
# those neighbours can be a row the predicate throws away, and the dense half returns fewer
# candidates than it asked for — or none — while the index is working perfectly. Iterative
# scan keeps walking until it has enough rows that survive.
#
# The predicate that matters is not the document scope. It is RLS, and it is on every query
# this system makes: see `dense`.
ITERATIVE_SCAN = "relaxed_order"

# Reciprocal Rank Fusion. `k` damps the influence of the very top positions, so a chunk
# ranked first by one half and absent from the other does not automatically beat a chunk
# ranked third by both. 60 is the value from the original paper and the one M0 measured at.
RRF_K = 60


@dataclass(frozen=True, slots=True)
class Hit:
    chunk_id: UUID
    document_id: UUID
    filename: str
    #: How this document is opened and highlighted. A paginated one gets a page and boxes;
    #: a text one gets the character range. The client picks the viewer from this rather
    #: than from the filename, because an extension is a guess and this is a fact.
    media_type: str
    #: `None` for a document with no pages.
    page_num: int | None
    #: Offsets into the stored text unit — the page for a PDF, the whole file for a text
    #: document. What a text citation highlights with; meaningless as a highlight in a PDF,
    #: where pdfplumber's text and pdf.js's text layer do not agree.
    char_start: int
    char_end: int
    text: str
    bboxes: list[dict[str, float]]
    # Positions rather than scores. `ts_rank_cd` and cosine distance live on different,
    # corpus-dependent scales; keeping both raw makes the response self-describing without
    # implying they are comparable.
    lexical_rank: int | None
    dense_rank: int | None
    score: float
    # The magnitudes behind those positions. Not for ranking — see `fuse`, which never
    # sees them — but because `query_citations` has four score columns and a rank cannot
    # tell you whether third place was a strong third or the least bad of a bad set.
    lexical_score: float | None = None
    dense_score: float | None = None
    rerank_score: float | None = None
    # The document's labels, so a result can say what it is filed under. Read from
    # `documents.label_ids` rather than the chunk's copy: the two are kept in step by a
    # trigger, and the document's is what the citation refers to.
    #
    # Defaulted because it is a display concern. A test about prompt construction or
    # citation binding should not have to invent one to say what those functions do.
    label_ids: list[UUID] = field(default_factory=list[UUID])


def scoped(clause: str, documents: list[UUID] | None) -> str:
    """The document filter, appended to a `WHERE` that already exists.

    A narrowing filter and nothing else: it is applied *on top of* the policies, never
    instead of them, and it can only ever remove rows the caller was already entitled to.
    The `chunks` alias is deliberate — filtering `chunk_embeddings.document_id` would work
    and would be wrong, because that table's policy is tenant-scoped only and the join to
    `chunks` is what carries label isolation.
    """
    return f"{clause} AND c.document_id = ANY(:documents)" if documents else clause


def scope_params(documents: list[UUID] | None) -> dict[str, object]:
    return {"documents": [str(document) for document in documents]} if documents else {}


async def lexical(
    session: AsyncSession,
    question: str,
    limit: int = CANDIDATES,
    documents: list[UUID] | None = None,
) -> list[tuple[UUID, float]]:
    """Exact terms: identifiers, product codes, proper nouns, acronyms.

    `ts_rank_cd` rather than `ts_rank` because it accounts for term proximity. For an
    identifier query the match is exact either way; for prose, proximity is what separates a
    passage *about* the subject from one that merely mentions it.

    The score comes back alongside the id and goes nowhere near fusion — it is logged, in
    `query_citations.score_bm25`, and that is the only thing it is for.
    """
    if engine() == "bm25":
        return await _bm25(session, question, limit, documents)

    query = await to_tsquery(session, question)
    if not query:
        return []

    rows = await session.execute(
        text(
            "SELECT c.id, ts_rank_cd(c.tsv, q) AS score FROM chunks c, "
            "to_tsquery(:config, :query) q "
            f"{scoped('WHERE c.tsv @@ q', documents)} "
            "ORDER BY score DESC, c.id LIMIT :limit"
        ),
        {"config": CONFIGURATION, "query": query, "limit": limit, **scope_params(documents)},
    )
    return [(row.id, float(row.score)) for row in rows]


async def _projected(session: AsyncSession, embedding: list[float], space: Space) -> str:
    """The query vector, in the space's own basis, as a literal the search can bind.

    **The same rows project both sides.** `zenith_project` reads `embedding_space_axes` for
    the space it is handed, which is the space that will also filter the rows — so a query
    vector and the passages it is compared against go through one basis because there is only
    one, and it is in the database. Nothing in this process holds a matrix that could be
    stale, and migration 0027 explains why that is not merely tidier: numpy is deliberately
    absent from the shipped image, so a Python-side projection was never available anyway.

    **Its own round trip, and not folded into the `ORDER BY`.** `zenith_project` reads a
    table, so it is `STABLE` and not `IMMUTABLE`; a `STABLE` call on the right-hand side of
    `<=>` is not a constant, and pgvector's index scan wants one. Inlining it returns exactly
    the right rows from a sequential scan — the silent failure migration 0025 shipped and
    `test_vector_index.py` exists to catch. Measured at 0.79-0.87 ms warm in
    `eval/svd-basis.sql`, against a live search median of 941 ms.

    The result is cast to `halfvec(space.dimension)` by the caller, from the same `Space` that
    chose the basis. That cast is the last gate and the one that survives a new call site: a
    vector that did not come through this function is the wrong width, and pgvector raises
    rather than ranks.
    """
    projected = await session.scalar(
        text("SELECT zenith_project(CAST(:embedding AS vector), :model, :version)::text"),
        {"embedding": str(embedding), "model": space.model, "version": space.version},
    )
    if projected is None:
        # The space claims a projection and has no axes. `ck_embedding_spaces_projection_
        # complete` makes this hard to reach and a half-finished `fit-basis` is how it would
        # be reached anyway. Refused rather than fallen back to the unprojected vector,
        # because that fallback is precisely the confident nonsense this module is about: it
        # would be the right width by accident only when the projection is the identity.
        raise NoActiveEmbeddingSpace(
            f"space {space.model}/{space.version} declares a projection but has no basis"
        )
    return projected


async def dense(
    session: AsyncSession,
    embedding: list[float],
    space: Space,
    limit: int = CANDIDATES,
    ef_search: int | None = None,
    documents: list[UUID] | None = None,
) -> list[tuple[UUID, float]]:
    """Meaning: intent, synonyms, paraphrase — everything the lexical half cannot reach.

    Restricted to one embedding space. `embedding_spaces` exists so several can coexist
    during a reindex (RNF-08), and vectors from two models are not comparable — a query that
    forgot this filter would rank across incompatible spaces and return confident nonsense.

    **One `Space`, not a model and a version and a width.** That is the change migration 0027
    required and it is the whole mechanism: the same value projects the query vector, filters
    the rows and decides the width both are cast to, so there is no second argument to pair
    incorrectly. `embeddings.space` has the argument in full. Since 0027 a stored vector is as
    wide as its space says, so the mismatch that used to rank is now
    `ERROR: different halfvec dimensions 1024 and 512` at the first row touched —
    demonstrated in `eval/svd-basis.sql`, not asserted.

    **The query vector is cast to `halfvec(k)`, and that cast is load-bearing.** Since
    migration 0025 the HNSW index is on `embedding_half`, the fp16 representation — three
    times smaller per vector at index recall 1.0000 against exact, measured in
    `eval/quantisation.json`. An operator class covers one type: cast the query to `vector`
    and the distance expression no longer matches the index, so the planner falls back to a
    sequential scan over the whole corpus and everything above still returns the right rows,
    slower and more slowly the bigger the corpus gets. Nothing degrades, nothing is marked
    `degraded`, and no test can see it — a seeded corpus is far too small for the planner to
    prefer an index either way. The only check that means anything is `EXPLAIN` against a real
    installation, which is where this was verified:
    `Index Scan using ix_chunk_embeddings_hnsw_half on chunk_embeddings e`.

    `k` is `space.dimension` since 0027 rather than a literal 1024, and the index it has to
    match is partial — one per space, predicated on the same space filter this query already
    emits. So the cast, the index and the `WHERE` clause all come from one value, and a
    `Space` is the only way to supply it.
    """
    if ef_search:
        # A per-session knob, and a speed/recall trade, which is why the value comes from
        # the hardware profile. Left at pgvector's default, recall from the index itself
        # caps below what the data supports.
        await session.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))

    # Unconditional, and the condition this used to carry was the bug. It was set only when
    # a document scope was passed, reasoning that a query with no scope has no filter and so
    # has nothing to discard. **RLS is a filter.** Every query here runs under
    # `tenant_id = zenith_current_tenant()` on `chunk_embeddings` and, through the join,
    # `label_ids && zenith_current_labels()` on `chunks`. The HNSW graph is shared by every
    # tenant, so the scan takes its `ef_search` nearest neighbours from all of it and the
    # policies discard afterwards: an unscoped query is not an unfiltered one, and the dense
    # half hands fusion fewer candidates than it asked for while the index reports success.
    #
    # `eval/tenant-scale.json` (2026-08-28, `cpu`, ef_search 100, 13,549 embeddings, 42
    # questions) measured the stage: tenant-wide, 43.14 of 50 candidates on average and
    # *nothing at all* for 5 of 42 questions; under one ordinary label, 29.95 of 50 and
    # nothing for 10 of 42. With iterative scan, 50 of 50 and no empty question in either,
    # dense recall 0.8419 -> 0.9643. It is worst in the middle of the range: below roughly
    # 15% of the graph the planner abandons HNSW for an exact scan and the loss disappears
    # on its own, which is why a small corpus cannot see this and a growing one gets worse.
    #
    # `eval/iterative-scan.json` measured what it is worth end to end on the *unscoped*
    # path, which is what this line changes. The dense stage goes 42.07 -> 50.0 of 50 rows
    # and four questions stop coming back empty, for a median of 1.45 ms against 1.36 and a
    # p95 of 2.21 against 1.71. Recall@8 on the page does **not** move — 0.90 either way,
    # the same three questions missed — because the lexical and exact halves were covering
    # those four, which is the hybrid architecture doing its job (ADR 0002). What does move
    # is mean rank, 1.407 -> 1.370, and what stops is the masking being load-bearing: a
    # question with no lexical signal has no second half to fall back on. End-to-end p95 is
    # unchanged, 1653 ms against 1626, and cannot say more than that — the same arm varies
    # by 780 ms between identical passes, which is three hundred times the whole cost here.
    #
    # `hnsw.max_scan_tuples` is left alone, and that is a decision rather than an omission.
    # Iterative scan is not unbounded: pgvector 0.8 stops at `max_scan_tuples`, whose
    # default `SHOW` reports as 20,000 — above this entire graph, so no value written here
    # could bind on the corpus available to measure it, and an unmeasured constant is what
    # ADR 0005 says to refuse. The observed ceiling is nowhere near it anyway: 2.83 ms worst
    # of any single dense query, against a 10 s `statement_timeout`.
    #
    # `relaxed_order` rather than `strict_order`, and this pipeline fuses on positions, so
    # the concession is real: relaxed order can return rows slightly out of distance order.
    # It is measured and it is the right way round. `strict_order` returns *fewer* usable
    # rows — it lost `attention-optimizer` outright and took headline Recall@8 to 0.85 — for
    # a p95 of 3.20 ms against 2.21. An ordering RRF converts to ranks and a cross-encoder
    # then rescores is not worth a candidate.
    await session.execute(text(f"SET LOCAL hnsw.iterative_scan = {ITERATIVE_SCAN}"))

    # One embedding space only. `embedding_spaces` exists so several can coexist during a
    # reindex, and vectors from two models — or from two bases over one model — rank against
    # each other as confident nonsense.
    #
    # Since 0027 this predicate does a second job: it is the predicate of the space's partial
    # HNSW index, so it is also what selects the graph to walk. One clause, both jobs, from
    # one value.
    clause = "WHERE e.embedding_model = :model AND e.embedding_version = :version"

    # Projected through the space's own basis, or used as the model returned it. The two
    # branches produce vectors of different widths *on purpose*: that is what makes a query
    # aimed at the wrong space an error rather than an answer.
    vector = await _projected(session, embedding, space) if space.projects else str(embedding)
    width = space.dimension

    rows = await session.execute(
        text(
            # Reported as *similarity* rather than distance, so both score columns in
            # `query_citations` read the same way round: bigger is better.
            #
            # Scored from the same expression it is ordered by, rather than from the fp32
            # column beside it. Two expressions would mean `score_vector` disagreeing with
            # the order the row came back in — and reading `embedding` per row would detoast
            # 4 KB the query has no other use for.
            # Both sides carry the width, and both come from `space.dimension`. The cast on
            # the *column* is not decoration either: since 0027 the index is an expression
            # index over `embedding_half::halfvec(k)`, so an uncast column no longer matches
            # it and the planner would fall back to a sequential scan returning the right
            # rows. That is why `test_vector_index.py` reads the plan.
            f"SELECT c.id, 1 - (e.embedding_half::halfvec({width}) "
            f"<=> CAST(:embedding AS halfvec({width}))) AS score "
            "FROM chunk_embeddings e "
            # Not decoration: `chunk_embeddings` is filtered by tenant only, so this join is
            # where label isolation is enforced for the dense half.
            #
            # Composite since migration 0026, and it is the foreign key rather than an
            # optimisation: `chunks` is partitioned by `tenant_id`, its primary key is
            # `(id, tenant_id)` and `chunk_id` alone no longer identifies a row. This is not
            # a tenant filter in application code — there is no tenant in it, only an
            # equality between two columns — and both sides are still pruned by their own
            # policies, which is what `eval/partition-swap.json` records.
            "JOIN chunks c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id "
            f"{scoped(clause, documents)} "
            f"ORDER BY e.embedding_half::halfvec({width}) "
            f"<=> CAST(:embedding AS halfvec({width})) LIMIT :limit"
        ),
        {
            "model": space.model,
            "version": space.version,
            "embedding": vector,
            "limit": limit,
            **scope_params(documents),
        },
    )
    return [(row.id, float(row.score)) for row in rows]


async def _bm25(
    session: AsyncSession,
    question: str,
    limit: int,
    documents: list[UUID] | None,
) -> list[tuple[UUID, float]]:
    """The same contract, resolved inside the index instead of over the whole corpus.

    `ts_rank_cd` has to score every matching row before `LIMIT` can choose, so its cost is
    linear in matches: 5,953 ms at 300,000 passages under the real policy. ParadeDB resolves
    the top N inside the index — a different algorithm, which is why the gap is ~130x and
    why no hardware closes it. Migration 0022 has the measurements and the argument.

    Isolation is not this function's to enforce and it does not try: `zenith_lexical_search`
    takes no tenant and no labels, and reads both from the session variables the policies
    read. There is no argument here through which another tenant's corpus can be asked for.

    The document scope stays a SQL filter rather than moving into the Tantivy query. It is
    not an isolation predicate — `_reachable` has already checked those documents are
    visible — so leaving it outside costs a filter over at most `want` rows and keeps the
    scoping rule in one place. Over-fetching covers the rows it discards.
    """
    want = limit * 4 if documents else limit
    rows = await session.execute(
        text(
            "SELECT chunk_id, score FROM zenith_lexical_search(:question, :want)"
            + (
                " WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id = ANY(:documents))"
                if documents
                else ""
            )
            + " LIMIT :limit"
        ),
        {"question": question, "want": want, "limit": limit, **scope_params(documents)},
    )
    return [(row.chunk_id, float(row.score)) for row in rows]


def candidates(
    lexical_ids: list[UUID],
    dense_ids: list[UUID],
    exact_ids: list[UUID] | None = None,
    limit: int | None = None,
) -> list[tuple[UUID, float]]:
    """Everything either half proposed, in fused order.

    The union rather than the fused top-N, because fusion was doing two jobs and is bad at
    one of them. Choosing candidates and ordering results are different problems: a passage
    only the lexical half found is *exactly* the case worth reranking, and it is exactly the
    case RRF discards.

    Measured: identifier questions scored 0% at rank 8, and two of six were not in the fused
    top-50 at all — found by the lexical half, ranked out of existence by agreement.

    **`limit` is the cut the caller will actually take, and passing it is what keeps the
    leader floor alive.** The reranker's budget is smaller than the union — 8 candidates
    against a union of up to 110 — so the caller truncates. Truncating *afterwards* silently
    undoes `_promote_leaders`: with no limit here nothing is ever missing, no leader is
    promoted, and the caller's slice is a plain RRF top-N, which is the exact ordering this
    function exists to avoid.

    It was not theoretical. Asked *"¿cuánto tiempo máximo puede durar la detención
    preventiva?"* over a Spanish legal corpus, the passage answering it — Constitución
    article 17, **rank 1 in the dense half** — scored 1/61 for its single first place while
    seven passages ranked mediocrely by *both* halves scored more, and the slice to 8 threw
    it away. The answer was assembled from the Código Penal instead.
    """
    exact_ids = exact_ids or []
    total = len(lexical_ids) + len(dense_ids) + len(exact_ids)
    return fuse(lexical_ids, dense_ids, limit=limit or total, exact_ids=exact_ids)


def fuse(
    lexical_ids: list[UUID],
    dense_ids: list[UUID],
    limit: int,
    exact_ids: list[UUID] | None = None,
) -> list[tuple[UUID, float]]:
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
    # The exact-identifier ranking is a third input to the same arithmetic rather than a
    # special case. It is short and precise, so its top position contributes the same
    # 1/(k+1) any other first place does — and the leader floor below guarantees its best
    # hit a seat, which is what actually rescues a buried identifier.
    rankings = [lexical_ids, dense_ids, *([exact_ids] if exact_ids else [])]
    scores: dict[UUID, float] = {}
    for ranking in rankings:
        for position, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1 / (RRF_K + position)

    ordered = sorted(scores.items(), key=lambda item: (-item[1], str(item[0])))
    return _promote_leaders(ordered[:limit], rankings, limit)


def _promote_leaders(
    top: list[tuple[UUID, float]],
    rankings: list[list[UUID]],
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
    leaders = [ranking[0] for ranking in rankings if ranking]
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
    lexical_scores: dict[UUID, float] | None = None,
    dense_scores: dict[UUID, float] | None = None,
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
            "SELECT c.id, c.document_id, d.filename, d.media_type, c.page_num, "
            "       c.char_start, c.char_end, c.text, c.bboxes, d.label_ids "
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
            media_type=row.media_type,
            page_num=row.page_num,
            char_start=row.char_start,
            char_end=row.char_end,
            text=row.text,
            bboxes=list(row.bboxes or []),
            label_ids=list(row.label_ids or []),
            lexical_rank=lexical_positions.get(row.id),
            dense_rank=dense_positions.get(row.id),
            score=dict(ranked)[row.id],
            lexical_score=(lexical_scores or {}).get(row.id),
            dense_score=(dense_scores or {}).get(row.id),
        )
        for row in rows
    ]
    # Re-sorted in Python: `= ANY(...)` does not preserve the order of the array, and the
    # fused order is the entire product of this feature.
    return sorted(hits, key=lambda hit: order[hit.chunk_id])
