"""The narrow boundary between retrieved source candidates and ranking providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from uuid import UUID


class Outcome(StrEnum):
    ASSESSED = "assessed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ScoreKind(StrEnum):
    RAW_RANK_SCORE = "raw_rank_score"
    MODEL_PROBABILITY = "model_probability"
    EXPECTED_GRADE = "expected_grade"
    EXPECTED_UTILITY = "expected_utility"


class CompletionState(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class JudgeCapabilities:
    ranking: bool = True
    ordered_grade_distribution: bool = False
    binary_evidence_property: bool = False
    usage_reporting: bool = False
    reported_model_identity: bool = False


@dataclass(frozen=True, slots=True)
class Candidate:
    id: UUID
    text: str


@dataclass(frozen=True, slots=True)
class Judgment:
    candidate_id: UUID
    outcome: Outcome
    rank_value: float | None
    score_kind: ScoreKind | None
    provider: str
    requested_model: str | None = None
    reported_model: str | None = None
    deployment_fingerprint: str | None = None
    input_fingerprint: str | None = None
    rubric_id: str | None = None
    rubric_hash: str | None = None
    utility_map_id: str | None = None
    grade_distribution: tuple[float, ...] | None = None
    provider_expected_grade: float | None = None
    provider_confidence: float | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class AssessmentBatch:
    requested_ids: tuple[UUID, ...]
    judgments: tuple[Judgment, ...]
    provider: str
    elapsed_ms: int
    reported_model: str | None = None
    usage: int | None = None
    queue_ms: int | None = None
    completion_state: CompletionState = CompletionState.COMPLETE
    provider_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if self.elapsed_ms < 0 or self.queue_ms is not None and self.queue_ms < 0:
            raise ValueError("negative assessment timing")
        if self.usage is not None and (type(self.usage) is not int or self.usage < 0):
            raise ValueError("invalid assessment usage")
        ids = [item.candidate_id for item in self.judgments]
        if len(set(self.requested_ids)) != len(self.requested_ids):
            raise ValueError("duplicate requested candidate identity")
        if len(ids) != len(self.requested_ids) or set(ids) != set(self.requested_ids):
            raise ValueError("judgments must partition requested candidates")
        for item in self.judgments:
            if item.rank_value is not None and (
                isinstance(item.rank_value, bool) or not isfinite(item.rank_value)
            ):
                raise ValueError("rank must be a finite number")
            if item.outcome is Outcome.ASSESSED and (
                item.rank_value is None or item.score_kind is None
            ):
                raise ValueError("assessed candidate needs a rank and score kind")
            if item.outcome is not Outcome.ASSESSED and item.rank_value is not None:
                raise ValueError("unassessed candidate cannot have a rank")


class Judge(ABC):
    capabilities: JudgeCapabilities

    @abstractmethod
    async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
        """Assess exactly these candidates, preserving their source identities."""
