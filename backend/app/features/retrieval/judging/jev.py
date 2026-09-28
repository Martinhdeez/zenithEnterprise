"""Optional, fail-closed Jev pair assessor. No product route selects it by default."""

import asyncio
import hashlib
import json
import math
import re
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import cast

import httpx

from app.core.config import settings
from app.features.retrieval.judging.protocol import (
    AssessmentBatch,
    Candidate,
    CompletionState,
    Judge,
    JudgeCapabilities,
    Judgment,
    Outcome,
    ScoreKind,
)
from app.features.retrieval.judging.rubrics import (
    NOUL,
    SCORE6,
    UTILITY_MAP_ID,
    Formulation,
    Rubric,
    expected_utility,
)

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
QUESTION_ID = "contribution"
MAX_RESPONSE_BYTES = 65_536
MAX_STATE_BYTES = 16_384
MODEL_ID = re.compile(r"^jev-[0-9]+\.[0-9]+\.[0-9]+$")
PROBABILITY_TOLERANCE = 0.0001
# The API rounds both six displayed probabilities and the score to hundredths.
# At most 0.005 * sum(0..5) + 0.005 = 0.08 grade points can be hidden by that
# display precision. The small margin below covers binary float representation.
SCORE_TOLERANCE = 0.081


class Purpose(StrEnum):
    RERANKING = "reranking"
    SEGMENTATION = "segmentation"
    CLAIM_SUPPORT = "claim_support"


@dataclass(frozen=True, slots=True)
class ProcessingPolicy:
    reranking: bool = False
    segmentation: bool = False
    claim_support: bool = False

    def allows(self, purpose: Purpose) -> bool:
        return {
            Purpose.RERANKING: self.reranking,
            Purpose.SEGMENTATION: self.segmentation,
            Purpose.CLAIM_SUPPORT: self.claim_support,
        }[purpose]


class JevFailure(Exception):
    """Safe provider failure; never includes a source-bearing response or a key."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class JevQuota:
    """Single-process budget. Enabling multiple API workers requires a shared limiter."""

    def __init__(
        self,
        max_requests: int,
        max_input_tokens: int,
        *,
        max_concurrency: int = 2,
        max_pending: int = 8,
        queue_timeout: float = 2.0,
        breaker_failures: int = 3,
        breaker_cooldown: float = 30.0,
    ) -> None:
        if max_requests < 0 or max_input_tokens < 0:
            raise ValueError("negative Jev quota")
        if min(max_concurrency, max_pending, breaker_failures) < 1:
            raise ValueError("invalid Jev capacity")
        if queue_timeout <= 0 or breaker_cooldown <= 0:
            raise ValueError("invalid Jev timeout")
        self.max_requests = max_requests
        self.max_input_tokens = max_input_tokens
        self.requests = 0
        self.input_tokens = 0
        self._lock = asyncio.Lock()
        self._capacity = asyncio.Semaphore(max_concurrency)
        self._max_pending = max_pending
        self._pending = 0
        self._queue_timeout = queue_timeout
        self._breaker_failures = breaker_failures
        self._breaker_cooldown = breaker_cooldown
        self._consecutive_failures = 0
        self._open_until = 0.0

    @asynccontextmanager
    async def slot(self) -> AsyncGenerator[None]:
        """Bound queued and in-flight requests across clients sharing this quota."""
        async with self._lock:
            if self._pending >= self._max_pending:
                raise JevFailure("queue_full")
            self._pending += 1
        acquired = False
        try:
            try:
                await asyncio.wait_for(self._capacity.acquire(), self._queue_timeout)
            except TimeoutError:
                raise JevFailure("queue_timeout") from None
            acquired = True
            yield
        finally:
            async with self._lock:
                self._pending -= 1
            if acquired:
                self._capacity.release()

    async def check_breaker(self) -> None:
        async with self._lock:
            if time.monotonic() < self._open_until:
                raise JevFailure("provider_circuit_open")

    async def record_provider_result(self, *, transient_failure: bool) -> None:
        async with self._lock:
            if transient_failure:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._breaker_failures:
                    self._open_until = time.monotonic() + self._breaker_cooldown
            else:
                self._consecutive_failures = 0
                self._open_until = 0.0

    async def reserve(self, estimated_tokens: int) -> None:
        async with self._lock:
            if (
                self.requests >= self.max_requests
                or self.input_tokens + estimated_tokens > self.max_input_tokens
            ):
                raise JevFailure("quota_exhausted")
            self.requests += 1
            self.input_tokens += estimated_tokens

    async def reconcile(self, estimated_tokens: int, actual_tokens: int | None) -> None:
        if actual_tokens is None:
            return  # Unknown usage cannot be counted as zero.
        async with self._lock:
            self.input_tokens += actual_tokens - estimated_tokens


Authorize = Callable[[str, Candidate, Purpose], Awaitable[bool]]


class JevJudge(Judge):
    capabilities = JudgeCapabilities(
        ranking=True,
        ordered_grade_distribution=True,
        binary_evidence_property=True,
        usage_reporting=True,
        reported_model_identity=True,
    )

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str = "jev-1.13.0",
        formulation: Formulation = Formulation.SCORE6,
        rubric: Rubric | None = None,
        policy: ProcessingPolicy | None = None,
        purpose: Purpose = Purpose.RERANKING,
        authorize: Authorize | None = None,
        quota: JevQuota | None = None,
        max_concurrency: int = 2,
        deadline_seconds: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not MODEL_ID.fullmatch(model):
            raise ValueError("Jev requires a pinned versioned model")
        if max_concurrency < 1 or deadline_seconds <= 0:
            raise ValueError("invalid Jev capacity")
        self.api_key = api_key
        self.model = model
        if rubric is not None and rubric.formulation is not formulation:
            raise ValueError("rubric formulation mismatch")
        self.rubric: Rubric = rubric or (SCORE6 if formulation is Formulation.SCORE6 else NOUL)
        self.policy = policy or ProcessingPolicy()
        self.purpose = purpose
        self.authorize = authorize
        self.quota = quota or JevQuota(0, 0, max_concurrency=max_concurrency)
        self.deadline_seconds = deadline_seconds
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3.0, read=10.0, write=3.0, pool=2.0),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=max_concurrency),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
        started = time.perf_counter()
        if len({item.id for item in candidates}) != len(candidates):
            raise ValueError("duplicate candidate identity")
        if not self.policy.allows(self.purpose) or self.authorize is None:
            raise JevFailure("processing_denied")
        if not self.api_key:
            raise JevFailure("missing_credential")
        judgments: list[Judgment] = []
        input_tokens = 0
        output_tokens = 0
        usage_known = True
        try:
            async with asyncio.timeout(self.deadline_seconds):
                # The overall deadline includes the initial scope check. Otherwise a
                # blocked policy store could hold the request forever before inference.
                if not all([await self._permitted(question, item) for item in candidates]):
                    raise JevFailure("processing_denied")
                for item in candidates:
                    try:
                        judgment, in_tokens, out_tokens = await self._assess_one(question, item)
                    except JevFailure as exc:
                        judgment = self._failed(item, exc.code)
                        in_tokens = out_tokens = None
                    judgments.append(judgment)
                    if in_tokens is None or out_tokens is None:
                        usage_known = False
                    else:
                        input_tokens += in_tokens
                        output_tokens += out_tokens
        except TimeoutError:
            judgments.extend(
                self._failed(item, "deadline") for item in candidates[len(judgments) :]
            )
            usage_known = False
        complete = all(j.outcome is Outcome.ASSESSED for j in judgments)
        return AssessmentBatch(
            requested_ids=tuple(item.id for item in candidates),
            judgments=tuple(judgments),
            provider="jev",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            reported_model=self.model if complete else None,
            usage=input_tokens if usage_known else None,
            output_tokens=output_tokens if usage_known else None,
            completion_state=CompletionState.COMPLETE if complete else CompletionState.PARTIAL,
            provider_fingerprint=hashlib.sha256(f"{ENDPOINT}\0{self.model}".encode()).hexdigest(),
        )

    async def _assess_one(
        self, question: str, candidate: Candidate
    ) -> tuple[Judgment, int | None, int | None]:
        state = {"original_question": question, "candidate_passage": candidate.text}
        request = {
            "model": self.model,
            "state": state,
            "questions": {QUESTION_ID: self.rubric.question()},
        }
        rendered = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
        if len(rendered) > MAX_STATE_BYTES:
            raise JevFailure("input_too_large")
        # UTF-8 byte count plus headroom is a conservative token reservation. Missing
        # provider usage retains the whole reservation rather than becoming a free call.
        reservation = len(rendered) + 4096
        try:
            async with self.quota.slot():
                await self.quota.check_breaker()
                if not await self._permitted(question, candidate):
                    raise JevFailure("processing_denied")
                await self.quota.reserve(reservation)
                async with self.client.stream(
                    "POST",
                    ENDPOINT,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    content=rendered,
                ) as response:
                    if response.status_code >= 300:
                        raise JevFailure(_http_failure(response.status_code))
                    parts: list[bytes] = []
                    size = 0
                    async for part in response.aiter_bytes():
                        size += len(part)
                        if size > MAX_RESPONSE_BYTES:
                            raise JevFailure("response_too_large")
                        parts.append(part)
            body = _strict_json(b"".join(parts))
            judgment, in_tokens, out_tokens = self._parse(body, candidate, rendered)
            await self.quota.reconcile(reservation, in_tokens)
            await self.quota.record_provider_result(transient_failure=False)
            return judgment, in_tokens, out_tokens
        except JevFailure as exc:
            if exc.code in {"rate_limited", "overloaded", "provider_error"}:
                await self.quota.record_provider_result(transient_failure=True)
            raise
        except (httpx.TimeoutException, httpx.NetworkError):
            await self.quota.record_provider_result(transient_failure=True)
            raise JevFailure("transport_unavailable") from None
        except httpx.HTTPError:
            await self.quota.record_provider_result(transient_failure=True)
            raise JevFailure("transport_unavailable") from None

    def _parse(
        self, body: object, candidate: Candidate, rendered: bytes
    ) -> tuple[Judgment, int | None, int | None]:
        root = _mapping(body)
        if root.get("model") != self.model:
            raise JevFailure("model_mismatch")
        answers = _mapping(root.get("answers"))
        if set(answers) != {QUESTION_ID}:
            raise JevFailure("answer_mismatch")
        answer = _mapping(answers[QUESTION_ID])
        expected_type = "score" if self.rubric.formulation is Formulation.SCORE6 else "noul"
        if answer.get("type") != expected_type:
            raise JevFailure("answer_type")
        in_tokens, out_tokens = _usage(root.get("usage"))
        fingerprint = hashlib.sha256(rendered).hexdigest()
        deployment = hashlib.sha256(f"{ENDPOINT}\0{self.model}".encode()).hexdigest()
        if self.rubric.formulation is Formulation.NOUL:
            value = _probability(answer.get("noul"))
            return (
                Judgment(
                    candidate_id=candidate.id,
                    outcome=Outcome.ASSESSED,
                    rank_value=value,
                    score_kind=ScoreKind.MODEL_PROBABILITY,
                    provider="jev",
                    requested_model=self.model,
                    reported_model=cast(str, root["model"]),
                    deployment_fingerprint=deployment,
                    input_fingerprint=fingerprint,
                    rubric_id=self.rubric.id,
                    rubric_hash=self.rubric.hash,
                ),
                in_tokens,
                out_tokens,
            )
        distribution = _distribution(answer.get("probabilities"), self.rubric)
        legend = _mapping(answer.get("legend"))
        if legend != {str(i): level for i, level in enumerate(self.rubric.levels or ())}:
            raise JevFailure("legend_mismatch")
        provider_score = _finite(answer.get("score"))
        expected = sum(index * probability for index, probability in enumerate(distribution))
        if abs(provider_score - expected) > SCORE_TOLERANCE:
            raise JevFailure("score_mismatch")
        confidence = _probability(answer.get("confidence"))
        utility = expected_utility(distribution)
        return (
            Judgment(
                candidate_id=candidate.id,
                outcome=Outcome.ASSESSED,
                rank_value=utility,
                score_kind=ScoreKind.EXPECTED_UTILITY,
                provider="jev",
                requested_model=self.model,
                reported_model=cast(str, root["model"]),
                deployment_fingerprint=deployment,
                input_fingerprint=fingerprint,
                rubric_id=self.rubric.id,
                rubric_hash=self.rubric.hash,
                grade_distribution=distribution,
                provider_expected_grade=provider_score,
                provider_confidence=confidence,
                utility_map_id=UTILITY_MAP_ID,
            ),
            in_tokens,
            out_tokens,
        )

    def _failed(self, candidate: Candidate, code: str) -> Judgment:
        return Judgment(
            candidate_id=candidate.id,
            outcome=Outcome.FAILED,
            rank_value=None,
            score_kind=None,
            provider="jev",
            requested_model=self.model,
            rubric_id=self.rubric.id,
            rubric_hash=self.rubric.hash,
            failure_code=code,
        )

    async def _permitted(self, question: str, candidate: Candidate) -> bool:
        if self.authorize is None:
            return False
        try:
            return await self.authorize(question, candidate, self.purpose)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - callback details may contain private source text
            raise JevFailure("processing_denied") from None


def _http_failure(status: int) -> str:
    if status in (301, 302, 303, 307, 308):
        return "redirect_denied"
    if status == 401:
        return "invalid_credential"
    if status == 429:
        return "rate_limited"
    if status == 529:
        return "overloaded"
    if status == 422:
        return "request_rejected"
    return "provider_error"


def _strict_json(data: bytes) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise JevFailure("duplicate_json_key")
            result[key] = value
        return result

    try:
        decoded: object = json.loads(data, object_pairs_hook=pairs)
    except (UnicodeError, ValueError, RecursionError):
        raise JevFailure("invalid_json") from None

    def depth(value: object, remaining: int) -> None:
        if remaining < 0:
            raise JevFailure("response_too_deep")
        if isinstance(value, dict):
            for child in cast(dict[str, object], value).values():
                depth(child, remaining - 1)
        elif isinstance(value, list):
            for child in cast(list[object], value):
                depth(child, remaining - 1)

    depth(decoded, 12)
    return decoded


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise JevFailure("invalid_response_shape")
    return cast(dict[str, object], value)


def _finite(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise JevFailure("invalid_number")
    return float(value)


def _probability(value: object) -> float:
    number = _finite(value)
    if not 0 <= number <= 1:
        raise JevFailure("invalid_probability")
    return number


def _distribution(value: object, rubric: Rubric) -> tuple[float, ...]:
    mapping = _mapping(value)
    levels = rubric.levels or ()
    if set(mapping) != {str(index) for index in range(len(levels))}:
        raise JevFailure("grade_mismatch")
    probabilities = tuple(_probability(mapping[str(index)]) for index in range(len(levels)))
    if abs(sum(probabilities) - 1.0) > PROBABILITY_TOLERANCE:
        raise JevFailure("probability_mass")
    return probabilities


def _usage(value: object) -> tuple[int | None, int | None]:
    if value is None:
        return None, None
    data = _mapping(value)
    result: list[int] = []
    for key in ("input_tokens", "output_tokens"):
        count = data.get(key)
        if type(count) is not int or count < 0:
            raise JevFailure("invalid_usage")
        result.append(count)
    return result[0], result[1]


def configured_jev_judge(
    *,
    formulation: Formulation,
    rubric: Rubric | None = None,
    purpose: Purpose,
    authorize: Authorize,
    quota: JevQuota | None = None,
    deadline_seconds: float | None = None,
) -> JevJudge:
    """Deployment entrypoint; fail unless an operator accepted single-worker quotas."""
    if not settings.jev_single_worker_ack:
        raise JevFailure("topology_not_approved")
    return JevJudge(
        api_key=settings.jev_api_key.get_secret_value() if settings.jev_api_key else None,
        model=settings.jev_model,
        formulation=formulation,
        rubric=rubric,
        policy=ProcessingPolicy(
            reranking=settings.external_processing_for_reranking,
            segmentation=settings.external_processing_for_segmentation,
            claim_support=settings.external_processing_for_claim_support,
        ),
        purpose=purpose,
        authorize=authorize,
        quota=quota or _shared_jev_quota(),
        max_concurrency=settings.jev_max_concurrency,
        deadline_seconds=(
            deadline_seconds
            if deadline_seconds is not None
            else settings.jev_total_deadline_seconds
        ),
    )


@lru_cache(maxsize=1)
def _shared_jev_quota() -> JevQuota:
    """One budget for all configured clients in the acknowledged single API worker."""
    return JevQuota(
        settings.jev_max_requests,
        settings.jev_max_input_tokens,
        max_concurrency=settings.jev_max_concurrency,
        max_pending=settings.jev_max_pending,
        queue_timeout=settings.jev_queue_timeout_seconds,
    )
