"""Provider-neutral assessment of an explicitly identified candidate set."""

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Protocol
from uuid import UUID


class Outcome(StrEnum):
    ASSESSED = "assessed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class Candidate:
    id: UUID
    text: str


@dataclass(frozen=True, slots=True)
class Judgment:
    candidate_id: UUID
    outcome: Outcome
    rank_value: float | None


@dataclass(frozen=True, slots=True)
class AssessmentBatch:
    requested_ids: tuple[UUID, ...]
    judgments: tuple[Judgment, ...]
    provider: str
    elapsed_ms: int

    def __post_init__(self) -> None:
        if self.elapsed_ms < 0:
            raise ValueError("negative assessment timing")
        ids = [item.candidate_id for item in self.judgments]
        if len(set(self.requested_ids)) != len(self.requested_ids):
            raise ValueError("duplicate requested candidate identity")
        if len(ids) != len(self.requested_ids) or set(ids) != set(self.requested_ids):
            raise ValueError("judgments must partition requested candidates")
        for item in self.judgments:
            if item.outcome is Outcome.ASSESSED:
                if (
                    item.rank_value is None
                    or isinstance(item.rank_value, bool)
                    or not isfinite(item.rank_value)
                ):
                    raise ValueError("assessed candidate needs a finite rank")
            elif item.rank_value is not None:
                raise ValueError("unassessed candidate cannot have a rank")


class Judge(Protocol):
    async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
        """Assess exactly these candidates; higher rank values come first."""
        ...
