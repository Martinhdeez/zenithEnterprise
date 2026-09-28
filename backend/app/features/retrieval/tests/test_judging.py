"""Fixed-candidate contract tests; the fake is not a model benchmark."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from app.core.hardware import PROFILES
from app.features.retrieval.judging.fake import FakeJudge
from app.features.retrieval.judging.protocol import Candidate, CompletionState, ScoreKind
from app.features.retrieval.judging.tei import TeiJudge
from app.features.retrieval.reranker import RerankerUnavailable, TeiReranker


def client(handler: object) -> TeiReranker:
    return TeiReranker(
        url="http://rerank",
        profile=PROFILES["low-spec"],
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


def valid(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/info":
        return httpx.Response(200, json={"model_id": "cross-encoder/current"})
    count = len(httpx.Response(200, content=request.content).json()["texts"])
    return httpx.Response(200, json=[{"index": n, "score": float(n)} for n in range(count)])


async def test_tei_contract_keeps_identities_ties_and_model_identity() -> None:
    ids = [uuid4() for _ in range(3)]
    candidates = [Candidate(ids[0], "same"), Candidate(ids[1], "same"), Candidate(ids[2], "other")]
    batch = await TeiJudge(client(valid)).assess("question", candidates)

    assert batch.requested_ids == tuple(ids)
    assert [item.candidate_id for item in batch.judgments] == ids
    assert [item.rank_value for item in batch.judgments] == [0.0, 0.0, 1.0]
    assert all(item.score_kind is ScoreKind.RAW_RANK_SCORE for item in batch.judgments)
    assert batch.reported_model == "cross-encoder/current"
    assert batch.provider_fingerprint
    assert batch.completion_state is CompletionState.COMPLETE
    assert all(item.provider_confidence is None for item in batch.judgments)


@pytest.mark.parametrize(
    "body",
    [
        [{"index": 0, "score": 1.0}, {"index": 0, "score": 2.0}],
        [{"index": 0, "score": 1.0}],
        [{"index": 0, "score": 1.0}, {"index": 2, "score": 2.0}],
        [{"index": True, "score": 1.0}, {"index": 1, "score": 2.0}],
        [{"index": 0, "score": float("nan")}, {"index": 1, "score": 2.0}],
        [{"index": 0, "score": float("inf")}, {"index": 1, "score": 2.0}],
        [{"index": 0, "score": True}, {"index": 1, "score": 2.0}],
        {"wrong": "shape"},
    ],
)
async def test_malformed_tei_output_fails_the_whole_batch(body: object) -> None:
    def malformed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(body, allow_nan=True).encode())

    with pytest.raises(RerankerUnavailable):
        await client(malformed).rank("q", ["one", "two"])


async def test_oversized_tei_output_is_rejected() -> None:
    def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1000)

    with pytest.raises(RerankerUnavailable, match="oversized"):
        await client(oversized).rank("q", ["one"])


async def test_deadline_bounds_all_sequential_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.features.retrieval import reranker

    monkeypatch.setattr(reranker, "RERANK_TIMEOUT", 0.5)
    calls = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.3)
        count = len(httpx.Response(200, content=request.content).json()["texts"])
        return httpx.Response(200, json=[{"index": n, "score": 0.5} for n in range(count)])

    with pytest.raises(RerankerUnavailable, match="deadline"):
        await client(slow).rank("q", ["a", "b", "c", "d", "e"])
    assert calls >= 2


async def test_cancellation_stops_later_dispatch() -> None:
    started = asyncio.Event()
    calls = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.sleep(10)
        return httpx.Response(200, json=[{"index": 0, "score": 0.5}])

    task = asyncio.create_task(client(slow).rank("q", ["a", "b", "c", "d", "e"]))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == 1


async def test_fake_is_deterministic_and_labeled() -> None:
    candidate = Candidate(uuid4(), "passage")
    batch = await FakeJudge({"passage": 0.25}).assess("q", [candidate])
    assert batch.provider == "test-fake"
    assert batch.judgments[0].rank_value == 0.25
    assert batch.judgments[0].reported_model == "deterministic-test-fake"
