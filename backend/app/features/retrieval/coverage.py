"""Execution coverage is a source-accounting claim, not model confidence."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class CoverageReceipt:
    version: str
    strategy: Literal["direct", "hybrid"]
    coverage_method: Literal["eligible_scope_manifest", "candidate_set"]
    execution_status: Literal["complete", "partial", "unavailable", "canceled"]
    scope_fingerprint: str
    manifest_fingerprint: str | None
    eligible_units: int | None
    selected_units: int
    attempted_units: int
    assessed_units: int
    failed_units: int
    skipped_units: int
    assessment_windows: int
    manifest_assessment_complete: bool
    source_representation: str
    snapshot_status: Literal["unchanged", "changed", "unknown"]
    evidence_status: str
    provider: str | None
    model: str | None
    rubric_id: str | None
    degraded: bool
    fallback_provider: str | None
    fallback_strategy: str | None
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        counts = (
            self.selected_units,
            self.attempted_units,
            self.assessed_units,
            self.failed_units,
            self.skipped_units,
            self.assessment_windows,
        )
        if any(count < 0 for count in counts):
            raise ValueError("negative coverage count")
        if self.eligible_units is not None and self.eligible_units < 0:
            raise ValueError("negative eligible unit count")
        if self.manifest_assessment_complete and not (
            self.strategy == "direct"
            and self.coverage_method == "eligible_scope_manifest"
            and self.execution_status == "complete"
            and self.snapshot_status == "unchanged"
            and self.eligible_units is not None
            and self.assessed_units == self.eligible_units
            and self.failed_units == self.skipped_units == 0
        ):
            raise ValueError("manifest completeness requires assessed, unchanged eligible scope")


VERSION = "evidence-coverage-v1"
REPRESENTATION = "eligible_parsed_content"
