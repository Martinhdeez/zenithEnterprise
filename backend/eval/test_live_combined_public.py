"""Opt-in public synthetic generator, Jev ranking, and strict support smoke.

Run only under a frozen live authorization manifest. The fixture contains no
tenant documents; the account and source exist only in disposable PostgreSQL.
"""

import hashlib
import json
import os

import pytest

from app.core.config import settings
from app.core.hardware import PROFILES
from app.features.generation.adapters.openai_compatible import OpenAIProvider
from app.features.generation.service import AnswerService
from app.features.retrieval.judging.jev import (
    _startup_jev_quota,  # pyright: ignore[reportPrivateUsage]
)
from app.features.retrieval.service import SearchService
from app.features.retrieval.tests.test_search import profile_for, seed
from conftest import Account, WorkingEmbedder

MODEL = "llama3.2:3b"
ENDPOINT = "http://127.0.0.1:18084/v1"
QUESTION = "According to this document, what must a controller implement?"


@pytest.mark.skipif(
    os.environ.get("ZENITH_LIVE_COMBINED_PUBLIC") != "1",
    reason="requires approved public/synthetic Jev and local generator run",
)
async def test_local_generator_jev_ranking_and_strict_support(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not settings.jev_api_key:
        pytest.skip("Jev credential unavailable")
    document = await seed(account.tenant_id, account.default_label)
    profile = await profile_for(account)
    monkeypatch.setattr(settings, "evidence_judge_provider", "jev_noul")
    monkeypatch.setattr(settings, "external_processing_for_reranking", True)
    monkeypatch.setattr(settings, "external_processing_for_claim_support", True)
    monkeypatch.setattr(settings, "jev_single_worker_ack", True)
    monkeypatch.setattr(settings, "jev_max_requests", 16)
    monkeypatch.setattr(settings, "jev_max_input_tokens", 120_000)
    monkeypatch.setattr(settings, "jev_max_concurrency", 2)
    monkeypatch.setattr(settings, "strict_claim_support_enabled", True)
    _startup_jev_quota.cache_clear()
    search = SearchService(
        profile,
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        hardware=PROFILES["gpu"],
        judge_mode="jev_noul",
    )
    service = AnswerService(profile, provider=OpenAIProvider(ENDPOINT, MODEL), search=search)
    result = await service.answer(QUESTION, documents=[document])
    _, quota = _startup_jev_quota()
    print(
        json.dumps(
            {
                "model": result.model,
                "answer_sha256": hashlib.sha256(result.answer.encode()).hexdigest(),
                "abstained": result.abstained,
                "citations": len(result.citations),
                "degraded": result.degraded,
                "reason": result.reason,
                "support_status": result.support_status,
                "support_layers": [item.failure_layer for item in result.support_assessments],
                "jev_reserved_calls": quota.requests,
                "jev_accounted_input_tokens": quota.input_tokens,
                "retrieval_ms": result.took_retrieval_ms,
                "generation_ms": result.took_generation_ms,
                "support_ms": result.took_support_ms,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    assert result.model == MODEL
    assert quota.requests > 0
    assert quota.requests <= 16
    assert quota.input_tokens <= 120_000
    assert result.support_status in {"supported", "insufficient", "not_assessed"}
    assert all(citation.document_id == document for citation in result.citations)
    assert not result.abstained or not result.citations
