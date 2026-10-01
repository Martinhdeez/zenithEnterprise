"""Local contract, legacy ordering and fallback; no paid inference or model-quality claim."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from app.core.hardware import PROFILES
from app.features.retrieval.degradation import RERANKING_UNAVAILABLE
from app.features.retrieval.judging.protocol import AssessmentBatch, Candidate, Judgment, Outcome
from app.features.retrieval.judging.tei import TeiJudge
from app.features.retrieval.reranker import RerankerUnavailable, TeiReranker
from app.features.retrieval.service import SearchService
from conftest import Account, WorkingEmbedder

from .test_rerank import QUERY
from .test_search import profile_for, seed


def client(handler: object) -> TeiReranker:
    return TeiReranker(
        url="http://local-rerank",
        profile=PROFILES["low-spec"],
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


async def test_adapter_matches_legacy_order_and_request_including_repeated_text() -> None:
    bodies: list[dict[str, object]] = []

    def tied(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://local-rerank/rerank"
        payload = json.loads(request.content)
        bodies.append(payload)
        return httpx.Response(
            200,
            json=[{"index": i, "score": 0.7} for i in reversed(range(len(payload["texts"])))],
        )

    candidates = [Candidate(uuid4(), text) for text in ["igual", "igual", "otro"]]
    legacy = await client(tied).rank("pregunta", [item.text for item in candidates])
    assessed = await TeiJudge(client(tied)).assess("pregunta", candidates)
    assert bodies[0] == bodies[1]
    assert assessed.requested_ids == tuple(item.id for item in candidates)
    assert [item.candidate_id for item in assessed.judgments] == [
        candidates[item.index].id for item in legacy
    ]
    assert [item.rank_value for item in assessed.judgments] == [item.score for item in legacy]
    assert assessed.provider == "tei"
    assert assessed.elapsed_ms >= 0


async def test_empty_and_duplicate_candidates_do_not_dispatch() -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    judge = TeiJudge(client(forbidden))
    assert not (await judge.assess("q", [])).judgments
    candidate = Candidate(uuid4(), "one")
    with pytest.raises(ValueError, match="duplicate"):
        await judge.assess("q", [candidate, candidate])


@pytest.mark.parametrize(
    "body",
    [
        [{"index": 0, "score": 1.0}, {"index": 0, "score": 2.0}],
        [{"index": 0, "score": 1.0}],
        [{"index": 0, "score": 1.0}, {"index": 2, "score": 2.0}],
        [{"index": True, "score": 1.0}, {"index": 1, "score": 2.0}],
        [{"index": "0", "score": 1.0}, {"index": 1, "score": 2.0}],
        [{"index": 0, "score": float("nan")}, {"index": 1, "score": 2.0}],
        [{"index": 0, "score": float("inf")}, {"index": 1, "score": 2.0}],
        [{"index": 0, "score": True}, {"index": 1, "score": 2.0}],
        [{"index": 0, "score": "0.7"}, {"index": 1, "score": 2.0}],
        {"wrong": "shape"},
        [None, None],
    ],
)
async def test_malformed_tei_output_rejects_the_entire_assessment(body: object) -> None:
    def malformed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(body, allow_nan=True).encode())

    with pytest.raises(RerankerUnavailable):
        await TeiJudge(client(malformed)).assess(
            "q", [Candidate(uuid4(), "one"), Candidate(uuid4(), "two")]
        )


async def test_deadline_bounds_all_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.features.retrieval import reranker

    monkeypatch.setattr(reranker, "RERANK_TIMEOUT", 0.1)
    calls = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.06)
        count = len(json.loads(request.content)["texts"])
        return httpx.Response(200, json=[{"index": i, "score": 0.5} for i in range(count)])

    with pytest.raises(RerankerUnavailable, match="deadline"):
        await TeiJudge(client(slow)).assess("q", [Candidate(uuid4(), str(i)) for i in range(5)])
    assert calls == 2


async def test_cancellation_prevents_later_dispatch() -> None:
    started = asyncio.Event()
    calls = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.sleep(10)
        return httpx.Response(200, json=[])

    task = asyncio.create_task(
        TeiJudge(client(slow)).assess("q", [Candidate(uuid4(), str(i)) for i in range(5)])
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == 1


@pytest.mark.parametrize("wrong", ["partial", "different-set"])
async def test_incomplete_or_wrong_assessment_preserves_fused_fallback(
    account: Account, wrong: str
) -> None:
    await seed(account.tenant_id, account.default_label)
    profile = await profile_for(account)
    fused = await SearchService(
        profile,
        hardware=PROFILES["low-spec"],
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
    ).search(QUERY, limit=50)  # type: ignore[arg-type]

    class InvalidJudge:
        async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
            ids = tuple(item.id for item in candidates)
            if wrong == "different-set":
                ids = tuple(uuid4() for _ in candidates)
            return AssessmentBatch(
                ids,
                tuple(Judgment(identity, Outcome.FAILED, None) for identity in ids),
                "test-only",
                0,
            )

    result = await SearchService(
        profile,
        hardware=PROFILES["cpu"],
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        judge=InvalidJudge(),
    ).search(QUERY, limit=50)  # type: ignore[arg-type]
    assert [hit.chunk_id for hit in result.hits] == [hit.chunk_id for hit in fused.hits]
    assert result.reason == RERANKING_UNAVAILABLE
    assert result.degraded


async def test_provider_response_order_cannot_change_ties(account: Account) -> None:
    await seed(account.tenant_id, account.default_label)
    profile = await profile_for(account)
    fused = await SearchService(
        profile,
        hardware=PROFILES["low-spec"],
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
    ).search(QUERY, limit=50)  # type: ignore[arg-type]

    class TiedJudge:
        async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
            return AssessmentBatch(
                tuple(item.id for item in candidates),
                tuple(Judgment(item.id, Outcome.ASSESSED, 0.9) for item in reversed(candidates)),
                "test-only",
                0,
            )

    result = await SearchService(
        profile,
        hardware=PROFILES["cpu"],
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        judge=TiedJudge(),
    ).search(QUERY, limit=50)  # type: ignore[arg-type]
    assert [hit.chunk_id for hit in result.hits] == [hit.chunk_id for hit in fused.hits]
    assert not result.degraded


def test_contract_rejects_missing_duplicate_or_nonfinite_judgments() -> None:
    identity = uuid4()
    for judgments in (
        (),
        (Judgment(uuid4(), Outcome.ASSESSED, 0.5),),
        (Judgment(identity, Outcome.ASSESSED, float("nan")),),
        (Judgment(identity, Outcome.ASSESSED, None),),
        (Judgment(identity, Outcome.SKIPPED, 0.5),),
    ):
        with pytest.raises(ValueError):
            AssessmentBatch((identity,), judgments, "test-only", 0)
