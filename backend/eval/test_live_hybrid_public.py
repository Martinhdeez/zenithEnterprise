"""Explicitly opted-in public-data Jev/TEI smoke through PostgreSQL and hybrid search.

Run only with ZENITH_RUN_LIVE_JEV_PUBLIC=1 and a securely supplied JEV_API_KEY.
This fixture contains public source-grounded paraphrases, never customer content.
"""

import json
import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.core.hardware import PROFILES
from app.features.retrieval.reranker import TeiReranker
from app.features.retrieval.service import SearchService
from app.features.retrieval.tests.test_search import profile_for, seed
from conftest import Account, WorkingEmbedder

FIXTURE = Path(__file__).parent / "fixtures/evidence-v3-public-v1.json"
QUESTIONS = ("lead-boiling", "shaking-location")


async def test_live_public_hybrid_comparison(
    account: Account, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if os.environ.get("ZENITH_RUN_LIVE_JEV_PUBLIC") != "1":
        pytest.skip("live public Jev run requires explicit opt-in")
    secret = os.environ.get("JEV_API_KEY")
    if not secret:
        pytest.skip("no securely supplied Jev credential")
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    passages = [(item["text"], index + 1) for index, item in enumerate(fixture["candidates"])]
    await seed(account.tenant_id, account.default_label, passages)
    profile = await profile_for(account)
    monkeypatch.setattr(settings, "jev_api_key", SecretStr(secret))
    monkeypatch.setattr(settings, "external_processing_for_reranking", True)
    monkeypatch.setattr(settings, "jev_single_worker_ack", True)
    monkeypatch.setattr(settings, "jev_max_requests", 16)
    monkeypatch.setattr(settings, "jev_max_input_tokens", 100_000)
    monkeypatch.setattr(settings, "jev_total_deadline_seconds", 35.0)
    tei_url = os.environ.get("ZENITH_PUBLIC_TEI_URL", "http://127.0.0.1:18082")
    rows: list[dict[str, object]] = []
    for query in fixture["queries"]:
        if query["id"] not in QUESTIONS:
            continue
        question = query["text"]
        baseline = await SearchService(
            profile,
            embedder=WorkingEmbedder(),  # type: ignore[arg-type]
            hardware=PROFILES["cpu"],
            reranker=TeiReranker(url=tei_url, profile=PROFILES["cpu"]),
        ).search(question)
        experimental = await SearchService(
            profile,
            embedder=WorkingEmbedder(),  # type: ignore[arg-type]
            hardware=PROFILES["cpu"],
            reranker=TeiReranker(url=tei_url, profile=PROFILES["cpu"]),
            judge_mode="jev_score6",
        ).search(question)
        assert baseline.assessment is not None and experimental.assessment is not None
        assert baseline.assessment.requested_ids == experimental.assessment.requested_ids
        rows.append(
            {
                "query_id": query["id"],
                "baseline_top_text": baseline.hits[0].text if baseline.hits else None,
                "baseline_relevance": baseline.relevance.value,
                "baseline_hits": len(baseline.hits),
                "experimental_top_text": experimental.hits[0].text if experimental.hits else None,
                "experimental_relevance": experimental.relevance.value,
                "experimental_hits": len(experimental.hits),
                "experimental_provider": experimental.assessment.provider
                if experimental.assessment
                else None,
                "experimental_model": experimental.assessment.reported_model
                if experimental.assessment
                else None,
                "experimental_fallback": experimental.fallback_provider,
                "experimental_reason": experimental.reason,
                "matched_candidate_count": len(experimental.assessment.requested_ids),
                "baseline_ms": baseline.took_ms,
                "experimental_ms": experimental.took_ms,
                "jev_input_tokens": experimental.assessment.usage
                if experimental.assessment and experimental.assessment.provider == "jev"
                else None,
            }
        )
    assert len(rows) == len(QUESTIONS)
    assert all(row["experimental_provider"] == "jev" for row in rows)
    output = tmp_path / "live-public-hybrid.json"
    output.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(rows, ensure_ascii=False))
