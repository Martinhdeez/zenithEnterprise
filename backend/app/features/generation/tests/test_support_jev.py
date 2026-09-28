"""The real Jev HTTP adapter under the distinct claim-processing purpose."""

import json
from uuid import uuid4

import httpx
import pytest

from app.features.auth.service import AccessProfile
from app.features.generation.answering import support
from app.features.retrieval.judging.jev import (
    JevJudge,
    JevQuota,
    ProcessingPolicy,
    Purpose,
)
from app.features.retrieval.judging.rubrics import CLAIM_SUPPORT_NOUL, Formulation
from app.features.retrieval.search import Hit
from app.features.tenancy.context import TenantContext


def source() -> Hit:
    body = "Residents pay 5%, subject to the stated exception."
    return Hit(
        chunk_id=uuid4(),
        document_id=uuid4(),
        filename="public.txt",
        media_type="text/plain",
        page_num=None,
        char_start=0,
        char_end=len(body),
        text=body,
        bboxes=[],
        lexical_rank=None,
        dense_rank=None,
        score=1.0,
        source_sha256="public-source-v1",
    )


async def test_claim_purpose_uses_real_jev_wire_and_versioned_rubric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, object]] = []

    async def current(*args: object, **kwargs: object) -> bool:
        return True

    async def permitted(query: str, candidate: object, purpose: Purpose) -> bool:
        assert purpose is Purpose.CLAIM_SUPPORT
        return True

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        assert request.url.host == "api.typesafe.ai"
        assert payload["model"] == "jev-1.13.0"
        assert payload["questions"]["contribution"]["type"] == "noul"
        assert "Every factual part" in payload["questions"]["contribution"]["criteria"]["true"]
        assert "Residents pay 5%" in payload["state"]["candidate_passage"]
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {"contribution": {"type": "noul", "noul": 0.94}},
                "usage": {"input_tokens": 120, "output_tokens": 5},
            },
        )

    monkeypatch.setattr(support, "verify_current", current)
    context = TenantContext.for_tenant(uuid4())
    profile = AccessProfile(uuid4(), context, frozenset({"query.execute"}))
    judge = JevJudge(
        api_key="synthetic-test-key",
        formulation=Formulation.NOUL,
        rubric=CLAIM_SUPPORT_NOUL,
        policy=ProcessingPolicy(claim_support=True),
        purpose=Purpose.CLAIM_SUPPORT,
        authorize=permitted,
        quota=JevQuota(2, 100_000),
        transport=httpx.MockTransport(respond),
    )
    try:
        reviewed = await support.review(
            "Residents pay 5% [1].",
            [source()],
            profile,
            context,
            max_claims=8,
            max_input_bytes=12000,
            min_noul=0.8,
            judge=judge,
        )
    finally:
        await judge.aclose()
    assert reviewed.status is support.SupportStatus.SUPPORTED
    assert reviewed.assessments[0].rubric_hash == CLAIM_SUPPORT_NOUL.hash
    assert reviewed.assessments[0].assessor_model == "jev-1.13.0"
    assert len(seen) == 1


async def test_claim_egress_denied_by_purpose_even_with_rerank_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = 0

    async def current(*args: object, **kwargs: object) -> bool:
        return True

    async def permitted(query: str, candidate: object, purpose: Purpose) -> bool:
        return True

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent += 1
        return httpx.Response(500)

    monkeypatch.setattr(support, "verify_current", current)
    context = TenantContext.for_tenant(uuid4())
    profile = AccessProfile(uuid4(), context, frozenset({"query.execute"}))
    judge = JevJudge(
        api_key="synthetic-test-key",
        formulation=Formulation.NOUL,
        rubric=CLAIM_SUPPORT_NOUL,
        policy=ProcessingPolicy(reranking=True, claim_support=False),
        purpose=Purpose.CLAIM_SUPPORT,
        authorize=permitted,
        quota=JevQuota(2, 100_000),
        transport=httpx.MockTransport(respond),
    )
    try:
        reviewed = await support.review(
            "Residents pay 5% [1].",
            [source()],
            profile,
            context,
            max_claims=8,
            max_input_bytes=12000,
            min_noul=0.8,
            judge=judge,
        )
    finally:
        await judge.aclose()
    assert reviewed.status is support.SupportStatus.NOT_ASSESSED
    assert sent == 0
