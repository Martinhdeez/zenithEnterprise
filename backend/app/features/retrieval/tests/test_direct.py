"""Bounded direct planner and honest coverage against application-role PostgreSQL."""

import asyncio
import time
from dataclasses import replace
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from app.common.exceptions import ConflictError
from app.core.config import settings
from app.core.database import owner_session, tenant_session
from app.core.hardware import PROFILES
from app.features.retrieval import search as search_module
from app.features.retrieval import service as service_module
from app.features.retrieval.direct import plan
from app.features.retrieval.judging.fake import FakeJudge
from app.features.retrieval.judging.protocol import (
    AssessmentBatch,
    Candidate,
)
from app.features.retrieval.judging.tei import TeiJudge
from app.features.retrieval.relevance import Relevance
from app.features.retrieval.reranker import TeiReranker
from app.features.retrieval.service import SearchService
from conftest import Account, WorkingEmbedder

from .test_search import PASSAGES, profile_for, seed


@pytest.fixture(autouse=True)
def enable_direct_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "direct_enabled", True)
    # Disposable PostgreSQL can be cold/contended on Windows; deadline behavior has
    # dedicated short-budget tests below and must not make other cases timing-based.
    monkeypatch.setattr(settings, "direct_total_deadline_seconds", 120.0)


class NoEmbed:
    def __init__(self) -> None:
        self.calls = 0

    async def embed_query(self, question: str) -> list[float]:
        self.calls += 1
        raise AssertionError("direct search embedded before planning")


def fake(passages: list[tuple[str, int]]) -> FakeJudge:
    return FakeJudge({body: float(index) for index, (body, _) in enumerate(passages)})


async def test_cap_plus_one_and_scope_before_cap(account: Account) -> None:
    target = await seed(account.tenant_id, account.default_label, [("Scoped fact.", 1)])
    await seed(account.tenant_id, account.default_label, PASSAGES)
    context = (await profile_for(account)).context
    budgets = dict(
        max_units=1,
        max_windows=8,
        max_source_bytes=10_000,
        max_rendered_bytes=50_000,
        window_chars=1200,
        overlap_chars=100,
    )

    broad = await plan(context, None, "fact", **budgets)
    scoped = await plan(context, [target], "fact", **budgets)

    assert not broad.complete and broad.reason == "direct_unit_limit"
    assert broad.eligible_units is None  # cap+1 is not an exact corpus total
    assert scoped.complete and scoped.eligible_units == 1
    assert len(scoped.windows) == 1 and scoped.units[0].document_id == target


async def test_exact_unit_cap_succeeds_and_cap_plus_one_is_not_truncated(account: Account) -> None:
    await seed(account.tenant_id, account.default_label, [("One.", 1), ("Two.", 2)])
    context = (await profile_for(account)).context
    budgets = dict(
        max_units=2,
        max_windows=8,
        max_source_bytes=10_000,
        max_rendered_bytes=50_000,
        window_chars=1200,
        overlap_chars=100,
    )
    exact = await plan(context, None, "fact", **budgets)
    assert exact.complete and exact.eligible_units == 2

    await seed(account.tenant_id, account.default_label, [("Three.", 3)])
    exceeded = await plan(context, None, "fact", **budgets)
    assert not exceeded.complete and exceeded.reason == "direct_unit_limit"
    assert exceeded.eligible_units is None


async def test_direct_never_embeds_and_does_not_require_vectors(account: Account) -> None:
    document = await seed(account.tenant_id, account.default_label, [("Public source fact.", 1)])
    pending = await seed(account.tenant_id, account.default_label, [("Not ingested yet.", 2)])
    async with owner_session() as session:
        await session.execute(
            text(
                "DELETE FROM chunk_embeddings WHERE chunk_id IN "
                "(SELECT id FROM chunks WHERE document_id = :id)"
            ),
            {"id": document},
        )
        await session.execute(
            text("UPDATE documents SET status = 'pending' WHERE id = :id"),
            {"id": pending},
        )
    embedder = NoEmbed()
    result = await SearchService(
        await profile_for(account),
        embedder=embedder,  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        judge=fake([("Public source fact.", 1)]),
    ).search("fact", mode="direct")

    assert embedder.calls == 0
    assert len(result.hits) == 1 and result.hits[0].document_id == document
    assert result.receipt is not None
    assert result.receipt.manifest_assessment_complete
    assert result.receipt.eligible_units == result.receipt.assessed_units == 1
    assert result.relevance is Relevance.NOT_ASSESSED


async def test_huge_single_unit_is_explicitly_incomplete_without_embedding(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(account.tenant_id, account.default_label, [("long " * 10_000, 1)])
    monkeypatch.setattr(settings, "direct_max_source_bytes", 1_000)
    embedder = NoEmbed()
    result = await SearchService(
        await profile_for(account),
        embedder=embedder,  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        judge=FakeJudge({}),
    ).search("long", mode="direct")

    assert embedder.calls == 0 and result.hits == []
    assert result.receipt is not None
    assert not result.receipt.manifest_assessment_complete
    assert result.receipt.reason_codes[0] in {"direct_source_budget", "direct_window_limit"}
    assert result.receipt.eligible_units == 1


async def test_auto_falls_back_to_hybrid_with_candidate_only_receipt(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(account.tenant_id, account.default_label)
    monkeypatch.setattr(settings, "direct_max_units", 1)
    result = await SearchService(
        await profile_for(account),
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        judge=fake(PASSAGES),
    ).search("controller taxpayer employees", mode="auto")

    assert result.hits
    assert result.receipt is not None and result.receipt.strategy == "hybrid"
    assert result.receipt.fallback_strategy == "hybrid"
    assert not result.receipt.manifest_assessment_complete
    assert result.receipt.eligible_units is None
    assert "direct_unit_limit" in result.receipt.reason_codes


async def test_long_tail_windows_cover_the_complete_source_without_new_chunk_ids(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "¿" + "abcde " * 500
    document = await seed(account.tenant_id, account.default_label, [(body, 1)])
    monkeypatch.setattr(settings, "direct_window_chars", 1000)
    monkeypatch.setattr(settings, "direct_window_overlap_chars", 100)
    captured: list[Candidate] = []

    class CaptureJudge(FakeJudge):
        async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
            captured.extend(candidates)
            self.scores = {item.text: float(index) for index, item in enumerate(candidates)}
            return await super().assess(question, candidates)

    result = await SearchService(
        await profile_for(account),
        embedder=NoEmbed(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        judge=CaptureJudge({}),
    ).search("long tail", mode="direct")

    assert result.receipt is not None and result.receipt.manifest_assessment_complete
    assert result.receipt.assessment_windows == len(captured) > 1
    assert len({item.id for item in captured}) == len(captured)
    assert result.hits[0].document_id == document
    assert result.hits[0].char_end <= len(body)
    assert result.hits[0].bboxes == []  # a partial PDF page has no invented highlight box
    assert "".join(item.text for item in captured).count("¿") == 1


async def test_utf8_pair_budget_preserves_addressable_source(account: Account) -> None:
    body = "Señal pública. " * 100
    await seed(account.tenant_id, account.default_label, [(body, 1)])
    question = "¿Qué dice la señal?"
    planned = await plan(
        (await profile_for(account)).context,
        None,
        question,
        max_units=2,
        max_windows=32,
        max_source_bytes=10_000,
        max_rendered_bytes=180_000,
        window_chars=1200,
        overlap_chars=100,
        max_pair_bytes=500,
    )
    assert planned.complete and len(planned.windows) > 1
    assert all(
        len(question.encode()) + len(window.text.encode()) <= 500 for window in planned.windows
    )
    assert planned.windows[0].start == 0 and planned.windows[-1].end == len(body)
    assert all(
        left.end >= right.start
        for left, right in zip(planned.windows, planned.windows[1:], strict=False)
    )


async def test_unknown_local_model_limit_cannot_claim_direct_completion(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(account.tenant_id, account.default_label, [("A source fact.", 1)])

    async def unknown_limit(self: TeiReranker) -> int | None:
        return None

    monkeypatch.setattr(TeiReranker, "max_input_length", unknown_limit)
    result = await SearchService(
        await profile_for(account),
        embedder=NoEmbed(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        judge=TeiJudge(TeiReranker()),
    ).search("fact", mode="direct")
    assert result.hits == [] and result.degraded
    assert result.receipt is not None
    assert result.receipt.reason_codes == ("direct_model_limit_unverified",)
    assert not result.receipt.manifest_assessment_complete


async def test_local_direct_disables_tei_truncation_and_rejects_oversized_pair(
    account: Account,
) -> None:
    await seed(account.tenant_id, account.default_label, [("ﬃ" * 60 + " fact.", 1)])
    requests: list[dict[str, object]] = []

    def tei(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json={"max_input_length": 512, "model_id": "test-bert"})
        body = httpx.Response(200, content=request.content).json()
        requests.append(body)
        return httpx.Response(413, json={"error": "pair exceeds input limit"})

    reranker = TeiReranker(url="http://rerank", transport=httpx.MockTransport(tei))
    result = await SearchService(
        await profile_for(account),
        embedder=NoEmbed(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        reranker=reranker,
    ).search("fact", mode="direct")
    assert requests and all(request["truncate"] is False for request in requests)
    assert result.hits == [] and result.degraded
    assert result.receipt is not None and not result.receipt.manifest_assessment_complete


async def test_trimmed_source_uses_full_original_range_for_partial_window(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "abcdef" * 400
    document = await seed(account.tenant_id, account.default_label, [(body, 1)])
    async with owner_session() as session:
        await session.execute(
            text("UPDATE chunks SET char_start = 10, char_end = :end WHERE document_id = :id"),
            {"end": 10 + len(body) + 3, "id": document},
        )
    monkeypatch.setattr(settings, "direct_window_chars", 1000)
    monkeypatch.setattr(settings, "direct_window_overlap_chars", 100)

    class EqualJudge(FakeJudge):
        async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
            self.scores = {item.text: 1.0 for item in candidates}
            return await super().assess(question, candidates)

    result = await SearchService(
        await profile_for(account),
        embedder=NoEmbed(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        judge=EqualJudge({}),
    ).search("fact", mode="direct")

    assert result.receipt is not None and result.receipt.manifest_assessment_complete
    assert result.hits[0].text == body
    assert (result.hits[0].char_start, result.hits[0].char_end) == (10, 10 + len(body) + 3)
    assert result.hits[0].bboxes  # the full original chunk keeps its stored page geometry


async def test_source_version_change_prevents_complete_receipt(
    account: Account,
) -> None:
    document = await seed(account.tenant_id, account.default_label, [("A stable passage.", 1)])

    class MutatingJudge(FakeJudge):
        async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
            async with owner_session() as session:
                await session.execute(
                    text("UPDATE documents SET sha256 = :hash WHERE id = :id"),
                    {"hash": str(uuid4()), "id": document},
                )
            return await super().assess(question, candidates)

    result = await SearchService(
        await profile_for(account),
        embedder=NoEmbed(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        judge=MutatingJudge({"A stable passage.": 1.0}),
    ).search("passage", mode="direct")

    assert result.hits == [] and result.degraded
    assert result.receipt is not None
    assert result.receipt.snapshot_status == "changed"
    assert not result.receipt.manifest_assessment_complete


async def test_new_eligible_source_during_assessment_invalidates_manifest(account: Account) -> None:
    await seed(account.tenant_id, account.default_label, [("Initial fact.", 1)])

    class AddingJudge(FakeJudge):
        async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
            await seed(account.tenant_id, account.default_label, [("Late fact.", 2)])
            return await super().assess(question, candidates)

    result = await SearchService(
        await profile_for(account),
        embedder=NoEmbed(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        judge=AddingJudge({"Initial fact.": 1.0}),
    ).search("fact", mode="direct")

    assert result.hits == [] and result.degraded
    assert result.receipt is not None
    assert result.receipt.snapshot_status == "changed"
    assert not result.receipt.manifest_assessment_complete


async def test_label_change_during_assessment_blocks_direct_disclosure(account: Account) -> None:
    document = await seed(account.tenant_id, account.default_label, [("Restricted fact.", 1)])

    class RelabelingJudge(FakeJudge):
        async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
            async with owner_session() as session:
                await session.execute(
                    text(
                        "UPDATE document_labels SET label_id = :finance "
                        "WHERE document_id = :document AND label_id = :default"
                    ),
                    {
                        "finance": account.finance_label,
                        "document": document,
                        "default": account.default_label,
                    },
                )
            return await super().assess(question, candidates)

    result = await SearchService(
        await profile_for(account, labels=(account.default_label,)),
        embedder=NoEmbed(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        judge=RelabelingJudge({"Restricted fact.": 1.0}),
    ).search("fact", mode="direct")

    assert result.hits == [] and result.degraded
    assert result.receipt is not None and not result.receipt.manifest_assessment_complete


async def test_hidden_source_is_absent_from_direct_manifest_and_receipt(account: Account) -> None:
    visible = await seed(account.tenant_id, account.default_label, [("Visible policy.", 1)])
    await seed(account.tenant_id, account.finance_label, [("Hidden payroll policy.", 2)])
    profile = await profile_for(account, labels=(account.default_label,))
    result = await SearchService(
        profile,
        embedder=NoEmbed(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        judge=FakeJudge({"Visible policy.": 1.0}),
    ).search("policy", mode="direct")

    assert [hit.document_id for hit in result.hits] == [visible]
    assert result.receipt is not None
    assert result.receipt.eligible_units == result.receipt.assessed_units == 1
    assert "payroll" not in str(result.receipt)


async def test_provider_failure_cannot_claim_direct_completion(account: Account) -> None:
    await seed(account.tenant_id, account.default_label, [("Available passage.", 1)])

    class FailingJudge(FakeJudge):
        async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
            raise TimeoutError

    result = await SearchService(
        await profile_for(account),
        embedder=NoEmbed(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        judge=FailingJudge({}),
    ).search("passage", mode="direct")

    assert result.hits == [] and result.degraded
    assert result.receipt is not None
    assert result.receipt.execution_status == "partial"
    assert not result.receipt.manifest_assessment_complete


async def test_auto_provider_failure_retains_reason_on_hybrid_fallback(account: Account) -> None:
    await seed(account.tenant_id, account.default_label, [("Fallback fact.", 1)])

    class FailingJudge(FakeJudge):
        async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
            raise RuntimeError("unavailable")

    result = await SearchService(
        await profile_for(account),
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        judge=FailingJudge({}),
    ).search("fallback", mode="auto")

    assert result.hits
    assert result.receipt is not None and result.receipt.strategy == "hybrid"
    assert result.receipt.fallback_strategy == "hybrid"
    assert "direct_assessment_incomplete" in result.receipt.reason_codes
    assert not result.receipt.manifest_assessment_complete


async def test_expired_return_check_reports_unknown_snapshot(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(account.tenant_id, account.default_label, [("Slow fact.", 1)])
    monkeypatch.setattr(settings, "direct_total_deadline_seconds", 2.0)

    class BlockingJudge(FakeJudge):
        async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
            time.sleep(2.1)
            return await super().assess(question, candidates)

    result = await SearchService(
        await profile_for(account),
        embedder=NoEmbed(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        judge=BlockingJudge({"Slow fact.": 1.0}),
    ).search("fact", mode="direct")

    assert result.hits == [] and result.degraded
    assert result.receipt is not None
    assert result.receipt.reason_codes == ("direct_deadline",)
    assert result.receipt.snapshot_status == "unknown"


async def test_cooperative_judge_timeout_keeps_deadline_reason(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(account.tenant_id, account.default_label, [("Slow cooperative fact.", 1)])
    monkeypatch.setattr(settings, "direct_total_deadline_seconds", 1.0)

    class SlowJudge(FakeJudge):
        async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
            await asyncio.sleep(1.2)
            return await super().assess(question, candidates)

    result = await SearchService(
        await profile_for(account),
        embedder=NoEmbed(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        judge=SlowJudge({"Slow cooperative fact.": 1.0}),
    ).search("fact", mode="direct")

    assert result.hits == [] and result.degraded
    assert result.receipt is not None
    assert result.receipt.reason_codes == ("direct_deadline",)
    assert result.receipt.snapshot_status == "unknown"


async def test_scope_check_obeys_direct_total_deadline(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = await seed(account.tenant_id, account.default_label, [("Scoped fact.", 1)])
    monkeypatch.setattr(settings, "direct_total_deadline_seconds", 0.1)
    service = SearchService(
        await profile_for(account),
        embedder=NoEmbed(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        judge=FakeJudge({"Scoped fact.": 1.0}),
    )

    async def slow_reachable(documents: list[UUID]) -> None:
        await asyncio.sleep(0.2)

    monkeypatch.setattr(service, "_reachable", slow_reachable)
    result = await service.search("fact", documents=[document], mode="direct")
    assert result.receipt is not None
    assert result.receipt.reason_codes == ("direct_deadline",)
    assert result.receipt.snapshot_status == "unknown"


async def test_predispatch_authorization_obeys_direct_total_deadline(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(account.tenant_id, account.default_label, [("Authorized fact.", 1)])
    monkeypatch.setattr(settings, "direct_total_deadline_seconds", 1.0)

    async def slow_verify(*args: object, **kwargs: object) -> bool:
        await asyncio.sleep(1.2)
        return True

    monkeypatch.setattr(service_module, "verify_current", slow_verify)
    result = await SearchService(
        await profile_for(account),
        embedder=NoEmbed(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        judge=FakeJudge({"Authorized fact.": 1.0}),
    ).search("fact", mode="direct")
    assert result.receipt is not None
    assert result.receipt.reason_codes == ("direct_deadline",)
    assert result.receipt.snapshot_status == "unknown"


async def test_direct_can_be_disabled_without_affecting_legacy(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "direct_enabled", False)
    service = SearchService(
        await profile_for(account),
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
    )
    with pytest.raises(ConflictError):
        await service.search("fact", mode="direct")
    with pytest.raises(ConflictError):
        await service.search("fact", mode="auto")
    assert (await service.search("fact", mode="legacy")).receipt is None


async def test_judge_response_for_another_candidate_cannot_be_disclosed(account: Account) -> None:
    await seed(account.tenant_id, account.default_label, [("Authorized fact.", 1)])

    class WrongIdentityJudge(FakeJudge):
        async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
            self.scores = {candidates[0].text: 1.0}
            return await super().assess(question, [Candidate(uuid4(), candidates[0].text)])

    result = await SearchService(
        await profile_for(account),
        embedder=NoEmbed(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        judge=WrongIdentityJudge({}),
    ).search("fact", mode="direct")

    assert result.hits == [] and result.degraded
    assert result.receipt is not None and not result.receipt.manifest_assessment_complete


async def test_cancellation_is_not_converted_to_a_partial_result(account: Account) -> None:
    await seed(account.tenant_id, account.default_label, [("Cancelable fact.", 1)])

    class CancelingJudge(FakeJudge):
        async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await SearchService(
            await profile_for(account),
            embedder=NoEmbed(),  # type: ignore[arg-type]
            hardware=PROFILES["cpu"],
            judge=CancelingJudge({}),
        ).search("fact", mode="direct")


async def test_direct_ties_keep_manifest_order_even_if_judge_reverses_results(
    account: Account,
) -> None:
    await seed(account.tenant_id, account.default_label, [("First fact.", 1), ("Second fact.", 2)])
    profile = await profile_for(account)
    expected = await plan(
        profile.context,
        None,
        "fact",
        max_units=settings.direct_max_units,
        max_windows=settings.direct_max_windows,
        max_source_bytes=settings.direct_max_source_bytes,
        max_rendered_bytes=settings.direct_max_rendered_bytes,
        window_chars=settings.direct_window_chars,
        overlap_chars=settings.direct_window_overlap_chars,
    )
    assert expected.complete and len(expected.units) == 2

    class ReversingJudge(FakeJudge):
        async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
            self.scores = {candidate.text: 1.0 for candidate in candidates}
            batch = await super().assess(question, candidates)
            return replace(batch, judgments=tuple(reversed(batch.judgments)))

    result = await SearchService(
        profile,
        embedder=NoEmbed(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        judge=ReversingJudge({}),
    ).search("fact", mode="direct")
    assert [hit.chunk_id for hit in result.hits] == [hit.chunk_id for hit in expected.units]


async def test_scoped_bm25_uses_rls_tsvector_before_ranking(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    scoped = await seed(account.tenant_id, account.default_label, [("severance allowance", 1)])
    await seed(account.tenant_id, account.default_label, [("severance allowance " * 10, 1)])
    monkeypatch.setattr(search_module, "engine", lambda: "bm25")

    async def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("globally cut BM25 results before applying the selected document")

    monkeypatch.setattr(search_module, "_bm25", forbidden)
    async with tenant_session((await profile_for(account)).context) as session:
        rows = await search_module.lexical(session, "severance allowance", 1, [scoped])
    assert len(rows) == 1
