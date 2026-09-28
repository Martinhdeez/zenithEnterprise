"""Opt-in real Jev strict-path smoke on disposable public synthetic source.

This checks adapter wiring, purpose permission, current application-role source
authorization, citation binding, and disclosure. Its permissive threshold is
for wire validation only; it does not measure semantic support quality.
"""

import os

import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.features.generation.adapters.mock import MockProvider
from app.features.generation.service import AnswerService
from app.features.retrieval.service import SearchService
from app.features.retrieval.tests.test_search import profile_for, seed
from conftest import Account, WorkingEmbedder

pytestmark = pytest.mark.skipif(
    os.environ.get("ZENITH_RUN_PUBLIC_STRICT") != "1", reason="requires approved public Jev call"
)


async def test_real_jev_support_assessment_rechecks_application_role_source(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = os.environ.get("ZENITH_JEV_API_KEY")
    if not key:
        pytest.skip("Jev credential not securely available")
    await seed(account.tenant_id, account.default_label)
    monkeypatch.setattr(settings, "strict_claim_support_enabled", True)
    monkeypatch.setattr(settings, "external_processing_for_claim_support", True)
    monkeypatch.setattr(settings, "jev_single_worker_ack", True)
    monkeypatch.setattr(settings, "jev_api_key", SecretStr(key))
    monkeypatch.setattr(settings, "jev_max_requests", 1)
    monkeypatch.setattr(settings, "jev_max_input_tokens", 50_000)
    monkeypatch.setattr(settings, "jev_max_concurrency", 1)
    monkeypatch.setattr(settings, "strict_support_min_noul", 0.0)
    profile = await profile_for(account)
    service = AnswerService(
        profile,
        provider=MockProvider(["Controllers implement technical measures [1]."]),
        search=SearchService(profile, embedder=WorkingEmbedder()),  # type: ignore[arg-type]
    )
    result = await service.answer("What must controllers implement?")
    assert result.support_status == "supported" and result.citations
    assert len(result.support_assessments) == 1
    assert result.support_assessments[0].assessor_provider == "jev"
    assert result.support_assessments[0].rubric_id == "zenith-claim-support-noul-v1"
