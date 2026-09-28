"""Strict support: deterministic clause checks and application-role disclosure."""

import asyncio
import hashlib
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.core.database import owner_session
from app.features.generation.adapters.mock import MockProvider
from app.features.generation.answering import support
from app.features.generation.service import AnswerService
from app.features.retrieval.judging.protocol import (
    AssessmentBatch,
    Candidate,
    Judge,
    JudgeCapabilities,
    Judgment,
    Outcome,
    ScoreKind,
)
from app.features.retrieval.judging.rubrics import CLAIM_SUPPORT_NOUL
from app.features.retrieval.search import Hit
from app.features.retrieval.service import SearchService
from app.features.retrieval.tests.test_search import profile_for, seed
from conftest import Account, WorkingEmbedder


def hit(body: str) -> Hit:
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
        source_sha256="source-v1",
    )


class FakeSupportJudge(Judge):
    capabilities = JudgeCapabilities(binary_evidence_property=True)

    def __init__(self, score: float = 0.95, *, delay: float = 0.0) -> None:
        self.score = score
        self.delay = delay
        self.calls: list[str] = []

    async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
        started = perf_counter()
        self.calls.append(question)
        if self.delay:
            await asyncio.sleep(self.delay)
        item = candidates[0]
        return AssessmentBatch(
            requested_ids=(item.id,),
            judgments=(
                Judgment(
                    candidate_id=item.id,
                    outcome=Outcome.ASSESSED,
                    rank_value=self.score,
                    score_kind=ScoreKind.MODEL_PROBABILITY,
                    provider="test-fake",
                    reported_model="deterministic-test-fake",
                    input_fingerprint=hashlib.sha256(item.text.encode()).hexdigest(),
                    rubric_id=CLAIM_SUPPORT_NOUL.id,
                    rubric_hash=CLAIM_SUPPORT_NOUL.hash,
                ),
            ),
            provider="test-fake",
            elapsed_ms=int((perf_counter() - started) * 1000),
        )


def test_conjunction_and_number_are_separate_claim_checks() -> None:
    source = hit("Residents pay 5%. Firms pay 6%.")
    prepared = support.prepare(
        "Residents pay 5% and firms pay 7% [1].", [source], max_claims=8, max_input_bytes=12000
    )
    assert len(prepared) == 2
    assert prepared[0].assessment.values_valid
    assert not prepared[1].assessment.values_valid
    assert prepared[1].assessment.support_status is support.SupportStatus.INSUFFICIENT
    assert prepared[0].assessment.claim_hash != prepared[1].assessment.claim_hash


def test_decimal_comma_and_point_are_equivalent_without_ignoring_percent_units() -> None:
    source = hit("La tasa aplicable es 1.5%.")
    equivalent = support.prepare(
        "La tasa aplicable es 1,5% [1].", [source], max_claims=8, max_input_bytes=12000
    )
    wrong_unit = support.prepare(
        "La tasa aplicable es 1,5 [1].", [source], max_claims=8, max_input_bytes=12000
    )
    assert equivalent[0].assessment.values_valid
    assert equivalent[0].assessment.failure_layer is None
    assert not wrong_unit[0].assessment.values_valid
    assert wrong_unit[0].assessment.failure_layer == "numeric_value"


def test_quote_presence_is_not_semantic_support() -> None:
    source = hit('The term "5%" applies only to residents.')
    prepared = support.prepare(
        'The "5%" applies to every firm [1].', [source], max_claims=8, max_input_bytes=12000
    )
    assert prepared[0].assessment.reference_valid
    assert prepared[0].assessment.span_valid
    assert prepared[0].assessment.values_valid
    assert prepared[0].assessment.support_status is support.SupportStatus.NOT_ASSESSED


def test_invalid_marker_quote_arithmetic_and_claim_cap_fail_closed() -> None:
    source = hit("The total is 5 and the rate is 6%.")
    wrong_marker = support.prepare(
        "The rate is 6% [2].", [source], max_claims=8, max_input_bytes=12000
    )
    wrong_quote = support.prepare(
        'The rate is "7%" [1].', [source], max_claims=8, max_input_bytes=12000
    )
    wrong_math = support.prepare("2 + 2 = 5 [1].", [source], max_claims=8, max_input_bytes=12000)
    capped = support.prepare("A [1]. B [1].", [source], max_claims=1, max_input_bytes=12000)
    assert not wrong_marker[0].assessment.reference_valid
    assert not wrong_quote[0].assessment.span_valid
    assert wrong_math[0].assessment.support_status is support.SupportStatus.CONTRADICTED
    assert capped[0].assessment.support_status is support.SupportStatus.NOT_ASSESSED
    assert capped[0].candidate.text == ""


def test_list_and_multiple_sources_keep_every_claim_and_context() -> None:
    first = hit("Controllers retain logs and keys.")
    second = hit("The exception is for students only.")
    prepared = support.prepare(
        "Controllers retain logs, keys, and backups [1]; the exception is for students [2].",
        [first, second],
        max_claims=8,
        max_input_bytes=12000,
    )
    assert len(prepared) == 3
    assert [claim.markers for claim in prepared] == [(1,), (1,), (2,)]
    assert all("Controllers retain logs and keys" in claim.candidate.text for claim in prepared)
    assert all("exception is for students" in claim.candidate.text for claim in prepared)
    assert all(claim.assessment.reference_valid for claim in prepared)


async def test_valid_citation_wrong_claim_is_not_disclosed(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(account.tenant_id, account.default_label)
    monkeypatch.setattr(settings, "strict_claim_support_enabled", True)
    model = MockProvider(
        [
            "Controllers need not implement technical measures [1].",
            "I cannot answer from the available sources.",
        ]
    )
    service = await service_for(account, model, FakeSupportJudge(score=0.1))
    result = await service.answer("What must controllers implement?")
    assert result.abstained and result.citations == []
    assert result.support_status == "insufficient"
    assert "need not" not in result.answer
    assert len(model.calls) == 2


async def service_for(account: Account, model: MockProvider, judge: Judge | None) -> AnswerService:
    profile = await profile_for(account)
    return AnswerService(
        profile,
        provider=model,
        search=SearchService(profile, embedder=WorkingEmbedder()),  # type: ignore[arg-type]
        support_judge=judge,
    )


async def test_supported_claim_and_strict_stream_are_buffered(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(account.tenant_id, account.default_label)
    monkeypatch.setattr(settings, "strict_claim_support_enabled", True)
    judge = FakeSupportJudge()
    service = await service_for(
        account, MockProvider(["Controllers implement technical measures [1]."]), judge
    )
    answer = await service.answer("What must controllers implement?")
    assert answer.support_status == "supported"
    assert answer.citations and not answer.abstained
    assert len(answer.support_assessments) == 1
    assert answer.support_assessments[0].rubric_id == CLAIM_SUPPORT_NOUL.id
    events = [item async for item in service.stream("What must controllers implement?")]
    assert len(events) == 2
    assert events[0].token == events[1].result.answer  # type: ignore[union-attr]
    assert len(judge.calls) == 2


async def test_no_assessor_withholds_uncertified_draft(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(account.tenant_id, account.default_label)
    monkeypatch.setattr(settings, "strict_claim_support_enabled", True)
    service = await service_for(
        account, MockProvider(["Controllers implement technical measures [1]."]), None
    )
    result = await service.answer("What must controllers implement?")
    assert result.abstained and result.citations == []
    assert result.answer == "I could not verify an answer against the available sources."
    assert result.support_status == "not_assessed" and result.reason == "support_unavailable"


async def test_wrong_value_repair_is_revalidated(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(account.tenant_id, account.default_label)
    monkeypatch.setattr(settings, "strict_claim_support_enabled", True)
    model = MockProvider(
        ["Controllers pay 8% [1].", "Controllers implement technical measures [1]."]
    )
    judge = FakeSupportJudge()
    service = await service_for(account, model, judge)
    result = await service.answer("What must controllers implement?")
    assert result.support_status == "supported" and not result.abstained
    assert "8%" not in result.answer
    assert len(model.calls) == 2 and judge.calls == ["Controllers implement technical measures"]
    assert (
        result.support_assessments[0].claim_hash
        == hashlib.sha256(b"Controllers implement technical measures").hexdigest()
    )


async def test_changed_source_during_assessment_withholds_draft(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = await seed(account.tenant_id, account.default_label)
    monkeypatch.setattr(settings, "strict_claim_support_enabled", True)

    class ChangingJudge(FakeSupportJudge):
        async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
            result = await super().assess(question, candidates)
            async with owner_session() as session:
                await session.execute(
                    text("UPDATE chunks SET text = 'Changed source.' WHERE document_id = :id"),
                    {"id": document},
                )
            return result

    service = await service_for(
        account, MockProvider(["Controllers implement technical measures [1]."]), ChangingJudge()
    )
    result = await service.answer("What must controllers implement?")
    assert result.abstained and result.citations == [] and result.consulted == []
    assert result.reason == "support_source_changed"


@pytest.mark.parametrize("fail_on_check", [1, 2])
async def test_failed_source_reauthorization_withholds_consulted_metadata(
    account: Account, monkeypatch: pytest.MonkeyPatch, fail_on_check: int
) -> None:
    await seed(account.tenant_id, account.default_label)
    monkeypatch.setattr(settings, "strict_claim_support_enabled", True)
    calls = 0

    async def unavailable(*args: object, **kwargs: object) -> bool:
        nonlocal calls
        calls += 1
        if calls == fail_on_check:
            raise RuntimeError("authorization database unavailable")
        return True

    monkeypatch.setattr(support, "verify_current", unavailable)
    service = await service_for(
        account, MockProvider(["Controllers implement technical measures [1]."]), FakeSupportJudge()
    )
    result = await service.answer("What must controllers implement?")
    assert result.abstained and result.citations == [] and result.consulted == []
    assert result.support_status == "not_assessed"
    assert result.reason == "support_source_unverified"
    assert "Controllers implement" not in result.answer


async def test_assessment_total_deadline_withholds_draft(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(account.tenant_id, account.default_label)
    monkeypatch.setattr(settings, "strict_claim_support_enabled", True)
    monkeypatch.setattr(settings, "strict_support_total_deadline_seconds", 0.01)
    service = await service_for(
        account,
        MockProvider(["Controllers implement technical measures [1]."]),
        FakeSupportJudge(delay=1.0),
    )
    result = await service.answer("What must controllers implement?")
    assert result.abstained and result.citations == []
    assert result.consulted == [] and result.reason == "support_source_unverified"


async def test_repair_cannot_reuse_old_support_after_source_change(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = await seed(account.tenant_id, account.default_label)
    monkeypatch.setattr(settings, "strict_claim_support_enabled", True)

    class RepairChangingProvider(MockProvider):
        async def complete(self, system: str, user: str):  # type: ignore[override]
            response = await super().complete(system, user)
            if len(self.calls) == 2:
                async with owner_session() as session:
                    await session.execute(
                        text("UPDATE chunks SET text = 'Changed source.' WHERE document_id = :id"),
                        {"id": document},
                    )
            return response

    model = RepairChangingProvider(
        ["Controllers pay 8% [1].", "Controllers implement technical measures [1]."]
    )
    service = await service_for(account, model, FakeSupportJudge())
    result = await service.answer("What must controllers implement?")
    assert result.abstained and result.citations == [] and result.consulted == []
    assert result.reason == "support_source_changed"
