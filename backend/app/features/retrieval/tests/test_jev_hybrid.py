"""Optional Jev hybrid orchestration against application-role PostgreSQL."""

import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from app.core.database import owner_session
from app.core.hardware import PROFILES
from app.features.retrieval import service as service_module
from app.features.retrieval.degradation import (
    EXTERNAL_JUDGE_UNAVAILABLE,
    RERANKING_UNAVAILABLE,
    SOURCE_CHANGED,
)
from app.features.retrieval.judging.jev import JevJudge, JevQuota, ProcessingPolicy
from app.features.retrieval.judging.rubrics import Formulation
from app.features.retrieval.relevance import Relevance
from app.features.retrieval.service import SearchService
from conftest import Account, WorkingEmbedder

from .test_jev import response
from .test_rerank import reranker, reverse_order
from .test_search import profile_for, seed


async def test_jev_keeps_semantic_evidence_with_zero_lexical_overlap(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(account.tenant_id, account.default_label)
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        body = response()
        answer = dict(body["answers"]["contribution"])  # type: ignore[index]
        text = payload["state"]["candidate_passage"]
        grade = 5 if "personnel records" in text else 0
        answer["score"] = float(grade)
        answer["probabilities"] = {str(i): int(i == grade) for i in range(6)}
        body["answers"] = {"contribution": answer}
        return httpx.Response(200, json=body)

    def factory(**kwargs: object) -> JevJudge:
        return JevJudge(
            api_key="synthetic-test-key",
            formulation=Formulation.SCORE6,
            policy=ProcessingPolicy(reranking=True),
            authorize=kwargs["authorize"],  # type: ignore[arg-type]
            quota=JevQuota(10, 100_000),
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(service_module, "configured_jev_judge", factory)
    result = await SearchService(
        await profile_for(account),
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        reranker=reranker(reverse_order),
        judge_mode="jev_score6",
    ).search("meaningful paraphrase absent from the three passages")

    assert len(seen) == 3
    assert result.hits
    assert result.hits[0].text.endswith("personnel records at any time.")
    assert result.hits[0].lexical_rank is None
    assert result.relevance is Relevance.NOT_ASSESSED
    assert result.evidence_status is not None and result.evidence_status.value == "not_assessed"
    assert result.assessment is not None and result.assessment.provider == "jev"
    assert not result.degraded


async def test_partial_jev_uses_complete_local_order(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(account.tenant_id, account.default_label)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(529 if calls == 2 else 200, json=response())

    def factory(**kwargs: object) -> JevJudge:
        return JevJudge(
            api_key="synthetic-test-key",
            formulation=Formulation.SCORE6,
            policy=ProcessingPolicy(reranking=True),
            authorize=kwargs["authorize"],  # type: ignore[arg-type]
            quota=JevQuota(10, 100_000),
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(service_module, "configured_jev_judge", factory)
    profile = await profile_for(account)
    local = await SearchService(
        profile,
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        reranker=reranker(reverse_order),
    ).search("controller taxpayer employees")
    experimental = await SearchService(
        profile,
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        reranker=reranker(reverse_order),
        judge_mode="jev_score6",
    ).search("controller taxpayer employees")

    assert calls == 3
    assert [hit.chunk_id for hit in experimental.hits] == [hit.chunk_id for hit in local.hits]
    assert experimental.fallback_provider == "tei"
    assert experimental.assessment is not None and experimental.assessment.provider == "tei"
    assert experimental.reason == EXTERNAL_JUDGE_UNAVAILABLE
    assert experimental.degraded


@pytest.mark.parametrize("mutation", ["text", "source_hash"])
async def test_source_change_during_queued_assessment_stops_export_and_disclosure(
    account: Account, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    document_id = await seed(account.tenant_id, account.default_label)
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            async with owner_session() as session:
                if mutation == "text":
                    await session.execute(
                        text("UPDATE chunks SET text = text || ' changed' WHERE document_id = :id"),
                        {"id": document_id},
                    )
                else:
                    await session.execute(
                        text("UPDATE documents SET sha256 = :hash WHERE id = :id"),
                        {"id": document_id, "hash": str(uuid4())},
                    )
        return httpx.Response(200, json=response())

    def factory(**kwargs: object) -> JevJudge:
        return JevJudge(
            api_key="synthetic-test-key",
            formulation=Formulation.SCORE6,
            policy=ProcessingPolicy(reranking=True),
            authorize=kwargs["authorize"],  # type: ignore[arg-type]
            quota=JevQuota(10, 100_000),
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(service_module, "configured_jev_judge", factory)
    result = await SearchService(
        await profile_for(account),
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        reranker=reranker(reverse_order),
        judge_mode="jev_score6",
    ).search("controller taxpayer employees")
    assert calls == 1
    assert result.hits == []
    assert result.reason == SOURCE_CHANGED
    assert result.degraded


async def test_label_change_during_assessment_prevents_export_and_disclosure(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id = await seed(account.tenant_id, account.default_label)
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            async with owner_session() as session:
                await session.execute(
                    text("DELETE FROM document_labels WHERE document_id = :id"),
                    {"id": document_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO document_labels (document_id, label_id) VALUES (:id, :label)"
                    ),
                    {"id": document_id, "label": account.finance_label},
                )
        return httpx.Response(200, json=response())

    def factory(**kwargs: object) -> JevJudge:
        return JevJudge(
            api_key="synthetic-test-key",
            formulation=Formulation.SCORE6,
            policy=ProcessingPolicy(reranking=True),
            authorize=kwargs["authorize"],  # type: ignore[arg-type]
            quota=JevQuota(10, 100_000),
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(service_module, "configured_jev_judge", factory)
    result = await SearchService(
        await profile_for(account, labels=(account.default_label,)),
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        reranker=reranker(reverse_order),
        judge_mode="jev_score6",
    ).search("controller taxpayer employees")
    assert calls == 1
    assert result.hits == []
    assert result.reason == SOURCE_CHANGED


async def test_failed_jev_and_local_reranker_keep_evidence_unassessed(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(account.tenant_id, account.default_label)

    def factory(**kwargs: object) -> JevJudge:
        return JevJudge(
            api_key="synthetic-test-key",
            formulation=Formulation.SCORE6,
            policy=ProcessingPolicy(reranking=True),
            authorize=kwargs["authorize"],  # type: ignore[arg-type]
            quota=JevQuota(10, 100_000),
            transport=httpx.MockTransport(lambda request: httpx.Response(529)),
        )

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("local model offline")

    monkeypatch.setattr(service_module, "configured_jev_judge", factory)
    result = await SearchService(
        await profile_for(account),
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        reranker=reranker(unavailable),
        judge_mode="jev_score6",
    ).search("meaningful paraphrase absent from the three passages")
    assert result.hits
    assert all(hit.lexical_rank is None for hit in result.hits)
    assert result.relevance is Relevance.NOT_ASSESSED
    assert result.degraded and result.reason == RERANKING_UNAVAILABLE
