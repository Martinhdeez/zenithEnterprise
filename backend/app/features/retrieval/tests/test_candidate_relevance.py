"""Corpus evidence survives reordering but cannot come from inaccessible documents."""

import json
from dataclasses import replace
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.hardware import PROFILES
from app.features.embeddings.space import Space
from app.features.retrieval import service as retrieval_service
from app.features.retrieval.relevance import Relevance
from app.features.retrieval.search import dense
from app.features.retrieval.service import SearchService
from conftest import Account, WorkingEmbedder
from conftest import account as seed_account

from .test_rerank import reranker
from .test_search import profile_for, seed


@pytest.fixture
async def other_account(configured_engines: None) -> Account:
    return await seed_account.__wrapped__(configured_engines)  # type: ignore[attr-defined]


def prefer_semantic(request: httpx.Request) -> httpx.Response:
    texts = json.loads(request.content)["texts"]
    return httpx.Response(
        200,
        json=[
            {"index": index, "score": 0.95 if body.startswith("Semantic") else 0.2}
            for index, body in enumerate(texts)
        ],
    )


async def test_semantic_selection_does_not_erase_authorized_lexical_evidence(
    account: Account,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This guard needs the complete seeded pool, not an ANN recall experiment over
    # every earlier tenant's tied one-hot vectors. Keep the real dense SQL and RLS,
    # but use its exact scan; vector index planning has separate acceptance tests.
    async def exact_dense(
        session: AsyncSession,
        embedding: list[float],
        space: Space,
        limit: int,
        ef_search: int | None,
        documents: list[UUID] | None,
    ) -> list[tuple[UUID, float]]:
        await session.execute(text("SET LOCAL enable_indexscan = off"))
        await session.execute(text("SET LOCAL enable_bitmapscan = off"))
        return await dense(session, embedding, space, limit, ef_search, documents)

    monkeypatch.setattr(retrieval_service, "dense", exact_dense)
    passages = [(f"The controller implements measure {i}.", i) for i in range(1, 5)] + [
        (f"Semantic paraphrase about safeguarding data, example {i}.", i) for i in range(5, 13)
    ]
    await seed(account.tenant_id, account.default_label, passages)
    service = SearchService(
        await profile_for(account),
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        hardware=replace(PROFILES["cpu"], rerank_candidates=32),
        reranker=reranker(prefer_semantic),
    )
    for limit in (3, 8):
        result = await service.search("controller", limit=limit)
        assert result.relevance is Relevance.CONFIDENT
        assert len(result.hits) == limit
        assert all(hit.lexical_rank is None for hit in result.hits)
        assert not result.degraded


async def test_high_rerank_scores_cannot_admit_a_query_with_no_lexical_evidence(
    account: Account,
) -> None:
    await seed(account.tenant_id, account.default_label, [("Semantic data protection.", 1)])
    result = await SearchService(
        await profile_for(account),
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        reranker=reranker(prefer_semantic),
    ).search("ZXQ-99817")
    assert result.relevance is Relevance.NONE
    assert not result.hits


async def test_another_tenants_lexical_matches_cannot_rescue_local_irrelevant_candidates(
    account: Account, other_account: Account
) -> None:
    await seed(account.tenant_id, account.default_label, [("Semantic data protection.", 1)])
    await seed(other_account.tenant_id, other_account.default_label, [("ZXQ-99817", 1)])
    result = await SearchService(
        await profile_for(account),
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        reranker=reranker(prefer_semantic),
    ).search("ZXQ-99817")
    assert result.relevance is Relevance.NONE
    assert not result.hits
