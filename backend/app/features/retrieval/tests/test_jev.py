"""HTTP contract tests for the optional external provider; no live calls."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from uuid import uuid4

import httpx
import pytest

from app.features.retrieval.judging.jev import (
    JevFailure,
    JevJudge,
    JevQuota,
    ProcessingPolicy,
    Purpose,
)
from app.features.retrieval.judging.protocol import Candidate, CompletionState, Outcome, ScoreKind
from app.features.retrieval.judging.rubrics import (
    NOUL,
    SCORE6,
    UTILITY_MAP_ID,
    Formulation,
)


async def permitted(question: str, candidate: Candidate, purpose: Purpose) -> bool:
    return True


def response(formulation: Formulation = Formulation.SCORE6) -> dict[str, object]:
    answer: dict[str, object]
    if formulation is Formulation.SCORE6:
        answer = {
            "type": "score",
            "score": 3.5,
            "legend": {str(i): description for i, description in enumerate(SCORE6.levels or ())},
            "probabilities": {"0": 0, "1": 0, "2": 0, "3": 0.5, "4": 0.5, "5": 0},
            "confidence": 0.7,
        }
    else:
        answer = {"type": "noul", "noul": 0.75}
    return {
        "model": "jev-1.13.0",
        "answers": {"contribution": answer},
        "usage": {"input_tokens": 210, "output_tokens": 20},
    }


def judge(
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
    *,
    formulation: Formulation = Formulation.SCORE6,
    authorize: Callable[[str, Candidate, Purpose], Awaitable[bool]] | None = permitted,
    policy: ProcessingPolicy | None = None,
    quota: JevQuota | None = None,
    key: str | None = "synthetic-test-key",
    deadline: float = 5.0,
) -> JevJudge:
    return JevJudge(
        api_key=key,
        formulation=formulation,
        authorize=authorize,
        policy=policy or ProcessingPolicy(reranking=True),
        quota=quota or JevQuota(10, 100_000),
        deadline_seconds=deadline,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


async def test_score_preserves_distribution_utility_identity_and_exact_export() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=response())

    candidate = Candidate(uuid4(), "A public source states the exception.")
    client = judge(handler)
    try:
        batch = await client.assess("Which rule has an exception?", [candidate])
    finally:
        await client.aclose()

    assert len(seen) == 1
    outbound = seen[0]
    assert str(outbound.url) == "https://api.typesafe.ai/v1/systemone"
    assert outbound.headers["authorization"] == "Bearer synthetic-test-key"
    payload = json.loads(outbound.content)
    assert payload["model"] == "jev-1.13.0"
    assert payload["state"] == {
        "original_question": "Which rule has an exception?",
        "candidate_passage": candidate.text,
    }
    assert payload["questions"]["contribution"] == SCORE6.question()
    item = batch.judgments[0]
    assert item.candidate_id == candidate.id
    assert item.score_kind is ScoreKind.EXPECTED_UTILITY
    assert item.rank_value == 3.5
    assert item.grade_distribution == (0.0, 0.0, 0.0, 0.5, 0.5, 0.0)
    assert item.provider_expected_grade == 3.5
    assert item.provider_confidence == 0.7
    assert item.utility_map_id == UTILITY_MAP_ID
    assert item.rubric_id == SCORE6.id and item.rubric_hash == SCORE6.hash
    assert item.input_fingerprint and item.deployment_fingerprint
    assert batch.usage == 210 and batch.output_tokens == 20
    assert batch.completion_state is CompletionState.COMPLETE


async def test_noul_uses_continuous_probability_without_threshold() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["questions"]["contribution"] == NOUL.question()
        return httpx.Response(200, json=response(Formulation.NOUL))

    client = judge(handler, formulation=Formulation.NOUL)
    try:
        batch = await client.assess("question", [Candidate(uuid4(), "public")])
    finally:
        await client.aclose()
    item = batch.judgments[0]
    assert item.rank_value == 0.75
    assert item.score_kind is ScoreKind.MODEL_PROBABILITY
    assert item.grade_distribution is None


async def test_rounded_provider_score_is_consistent_with_displayed_distribution() -> None:
    body = response()
    answer = dict(body["answers"]["contribution"])  # type: ignore[index]
    answer["score"] = 4.98
    answer["probabilities"] = {"0": 0, "1": 0, "2": 0, "3": 0, "4": 0.01, "5": 0.99}
    body["answers"] = {"contribution": answer}
    client = judge(lambda request: httpx.Response(200, json=body))
    try:
        batch = await client.assess("q", [Candidate(uuid4(), "public")])
    finally:
        await client.aclose()
    assert batch.judgments[0].outcome is Outcome.ASSESSED
    assert batch.judgments[0].rank_value == pytest.approx(4.99)
    assert batch.judgments[0].provider_expected_grade == 4.98


@pytest.mark.parametrize(
    "change",
    [
        {"model": "jev-older"},
        {"answers": {}},
        {"answers": {"contribution": {"type": "noul", "noul": 0.7}}},
        {"usage": {"input_tokens": True, "output_tokens": 20}},
        {"usage": {"input_tokens": -1, "output_tokens": 20}},
    ],
)
async def test_wrong_model_shape_type_or_usage_is_a_failed_judgment(
    change: dict[str, object],
) -> None:
    body = response() | change
    client = judge(lambda request: httpx.Response(200, json=body))
    try:
        batch = await client.assess("q", [Candidate(uuid4(), "public")])
    finally:
        await client.aclose()
    assert batch.completion_state is CompletionState.PARTIAL
    assert batch.judgments[0].outcome is Outcome.FAILED
    assert batch.judgments[0].rank_value is None


@pytest.mark.parametrize(
    "probabilities",
    [
        {"0": 1.0},
        {"0": 0.5, "1": 0.5, "2": 0, "3": 0, "4": 0, "5": 0, "01": 0},
        {"0": 0, "1": 0, "2": 0, "3": 0.4, "4": 0.4, "5": 0},
        {"0": 0, "1": 0, "2": 0, "3": True, "4": 0, "5": 0},
        {"0": 0, "1": 0, "2": 0, "3": -0.1, "4": 1.1, "5": 0},
    ],
)
async def test_score_rejects_bad_distributions(probabilities: dict[str, object]) -> None:
    body = response()
    answer = dict(body["answers"]["contribution"])  # type: ignore[index]
    answer["probabilities"] = probabilities
    body["answers"] = {"contribution": answer}
    client = judge(lambda request: httpx.Response(200, json=body))
    try:
        batch = await client.assess("q", [Candidate(uuid4(), "public")])
    finally:
        await client.aclose()
    assert batch.judgments[0].outcome is Outcome.FAILED


async def test_duplicate_json_keys_and_redirect_are_rejected_without_following() -> None:
    calls = 0

    def duplicate(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"model":"jev-1.13.0","model":"jev-1.13.0"}')

    first = judge(duplicate)
    try:
        batch = await first.assess("q", [Candidate(uuid4(), "public")])
    finally:
        await first.aclose()
    assert batch.judgments[0].failure_code == "duplicate_json_key"

    def redirect(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(307, headers={"Location": "https://attacker.example/steal"})

    second = judge(redirect)
    try:
        batch = await second.assess("q", [Candidate(uuid4(), "public")])
    finally:
        await second.aclose()
    assert calls == 1
    assert batch.judgments[0].failure_code == "redirect_denied"


async def test_denied_mixed_batch_and_missing_key_send_nothing() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=response())

    async def mixed(question: str, candidate: Candidate, purpose: Purpose) -> bool:
        return candidate.text == "public"

    candidates = [Candidate(uuid4(), "public"), Candidate(uuid4(), "private")]
    client = judge(handler, authorize=mixed)
    with pytest.raises(JevFailure, match="processing_denied"):
        await client.assess("q", candidates)
    await client.aclose()
    client = judge(handler, key=None)
    with pytest.raises(JevFailure, match="missing_credential"):
        await client.assess("q", candidates[:1])
    await client.aclose()
    client = judge(handler, policy=ProcessingPolicy(segmentation=True))
    with pytest.raises(JevFailure, match="processing_denied"):
        await client.assess("q", candidates[:1])
    await client.aclose()
    assert calls == 0


async def test_authorization_error_is_sanitized_and_exports_nothing() -> None:
    async def broken(question: str, candidate: Candidate, purpose: Purpose) -> bool:
        raise RuntimeError("private passage text must not escape")

    client = judge(lambda request: pytest.fail("no outbound request is allowed"), authorize=broken)
    try:
        with pytest.raises(JevFailure, match="^processing_denied$") as raised:
            await client.assess("q", [Candidate(uuid4(), "private")])
    finally:
        await client.aclose()
    assert "private passage" not in str(raised.value)


async def test_reauthorization_blocks_changed_candidate_before_dispatch() -> None:
    checks = 0
    calls = 0

    async def revoked(question: str, candidate: Candidate, purpose: Purpose) -> bool:
        nonlocal checks
        checks += 1
        return checks == 1

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=response())

    client = judge(handler, authorize=revoked)
    try:
        batch = await client.assess("q", [Candidate(uuid4(), "public")])
    finally:
        await client.aclose()
    assert checks == 2 and calls == 0
    assert batch.judgments[0].failure_code == "processing_denied"


async def test_quota_exhaustion_keeps_failed_rank_null() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=response())

    client = judge(handler, quota=JevQuota(1, 100_000))
    try:
        batch = await client.assess("q", [Candidate(uuid4(), "a"), Candidate(uuid4(), "b")])
    finally:
        await client.aclose()
    assert calls == 1
    assert [item.outcome for item in batch.judgments] == [Outcome.ASSESSED, Outcome.FAILED]
    assert batch.judgments[1].failure_code == "quota_exhausted"
    assert batch.judgments[1].rank_value is None


async def test_total_deadline_and_cancellation_stop_later_dispatch() -> None:
    calls = 0
    started = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.sleep(10)
        return httpx.Response(200, json=response())

    candidates = [Candidate(uuid4(), "a"), Candidate(uuid4(), "b")]
    client = judge(slow, deadline=0.05)
    try:
        batch = await client.assess("q", candidates)
    finally:
        await client.aclose()
    assert calls == 1
    assert all(item.failure_code == "deadline" for item in batch.judgments)

    started.clear()
    calls = 0
    client = judge(slow, deadline=20)
    task = asyncio.create_task(client.assess("q", candidates))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await client.aclose()
    assert calls == 1


async def test_total_deadline_includes_initial_authorization() -> None:
    async def stalled(question: str, candidate: Candidate, purpose: Purpose) -> bool:
        await asyncio.sleep(10)
        return True

    client = judge(
        lambda request: pytest.fail("authorization must finish before export"),
        authorize=stalled,
        deadline=0.05,
    )
    try:
        batch = await client.assess("q", [Candidate(uuid4(), "public")])
    finally:
        await client.aclose()
    assert batch.judgments[0].failure_code == "deadline"


async def test_shared_quota_bounds_concurrent_clients_and_queue() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return httpx.Response(200, json=response())

    shared = JevQuota(10, 100_000, max_concurrency=1, max_pending=1)
    first = judge(slow, quota=shared)
    second = judge(slow, quota=shared)
    task = asyncio.create_task(first.assess("q", [Candidate(uuid4(), "public")]))
    try:
        await started.wait()
        blocked = await second.assess("q", [Candidate(uuid4(), "public")])
        assert blocked.judgments[0].failure_code == "queue_full"
        assert calls == 1
        release.set()
        assert (await task).completion_state is CompletionState.COMPLETE
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await first.aclose()
        await second.aclose()


async def test_provider_breaker_is_shared_and_does_not_export_after_opening() -> None:
    calls = 0

    def overloaded(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(529)

    shared = JevQuota(10, 100_000, breaker_failures=2)
    first = judge(overloaded, quota=shared)
    second = judge(overloaded, quota=shared)
    try:
        for client in (first, second):
            batch = await client.assess("q", [Candidate(uuid4(), "public")])
            assert batch.judgments[0].failure_code == "overloaded"
        batch = await first.assess("q", [Candidate(uuid4(), "public")])
        assert batch.judgments[0].failure_code == "provider_circuit_open"
        assert calls == 2
    finally:
        await first.aclose()
        await second.aclose()
