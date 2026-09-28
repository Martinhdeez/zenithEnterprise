"""Optional R1 Jev boundary proposals over public/authorized extracted text.

This adapter only scores positions; it cannot rewrite source text or select a
production chunk. The caller owns source authorization for every rendered pair.
"""

from uuid import UUID

from app.features.ingestion.chunking.lossless import (
    BoundaryAssessment,
    Source,
    Span,
    boundary_id,
    validate_spans,
)
from app.features.retrieval.judging.jev import JevFailure, JevJudge, Purpose
from app.features.retrieval.judging.protocol import Candidate, Outcome, ScoreKind
from app.features.retrieval.judging.rubrics import BOUNDARY_NOUL

BOUNDARY_QUESTION = "Does the right source unit start a distinct thought at the marked boundary?"


async def assess_boundaries(
    source: Source,
    spans: tuple[Span, ...],
    judge: JevJudge,
    *,
    max_boundaries: int = 32,
    context_chars: int = 500,
    selected_ids: frozenset[UUID] | None = None,
) -> tuple[BoundaryAssessment, ...]:
    if judge.purpose is not Purpose.SEGMENTATION or judge.rubric != BOUNDARY_NOUL:
        raise ValueError("boundary calls require the segmentation purpose and versioned rubric")
    if max_boundaries < 1 or context_chars < 1:
        raise ValueError("invalid boundary budget")
    validate_spans(source, spans)
    pairs = list(zip(spans, spans[1:], strict=False))
    available = {boundary_id(left, right) for left, right in pairs}
    if selected_ids is not None and not selected_ids.issubset(available):
        raise ValueError("selected boundary does not belong to source")
    chosen = [
        (left, right)
        for left, right in pairs
        if selected_ids is None or boundary_id(left, right) in selected_ids
    ][:max_boundaries]
    chosen_ids = {boundary_id(left, right) for left, right in chosen}
    candidates = [
        Candidate(
            boundary_id(left, right),
            f"LEFT SOURCE CONTEXT:\n{source.text[max(0, left.end - context_chars) : left.end]}"
            f"\n<BOUNDARY>\nRIGHT SOURCE CONTEXT:\n"
            f"{source.text[right.start : min(len(source.text), right.start + context_chars)]}",
        )
        for left, right in chosen
    ]
    try:
        batch = await judge.assess(BOUNDARY_QUESTION, candidates) if candidates else None
    except JevFailure as exc:
        return tuple(
            BoundaryAssessment(
                boundary_id(left, right),
                source.identity,
                None,
                "unavailable",
                "jev",
                judge.model,
                BOUNDARY_NOUL.id,
                None,
                exc.code,
            )
            for left, right in pairs
        )
    by_id = {item.candidate_id: item for item in batch.judgments} if batch else {}
    result: list[BoundaryAssessment] = []
    for left, right in pairs:
        identity = boundary_id(left, right)
        item = by_id.get(identity)
        if identity not in chosen_ids:
            result.append(
                BoundaryAssessment(
                    identity,
                    source.identity,
                    None,
                    "unavailable",
                    "jev",
                    judge.model,
                    BOUNDARY_NOUL.id,
                    None,
                    "boundary_budget",
                )
            )
        elif item is not None and (
            item.outcome is Outcome.ASSESSED
            and item.score_kind is ScoreKind.MODEL_PROBABILITY
            and item.rubric_id == BOUNDARY_NOUL.id
            and item.rubric_hash == BOUNDARY_NOUL.hash
            and item.rank_value is not None
        ):
            result.append(
                BoundaryAssessment(
                    identity,
                    source.identity,
                    item.rank_value,
                    "assessed",
                    "jev",
                    item.reported_model,
                    item.rubric_id,
                    item.input_fingerprint,
                )
            )
        else:
            result.append(
                BoundaryAssessment(
                    identity,
                    source.identity,
                    None,
                    "unavailable" if item and item.outcome is Outcome.FAILED else "malformed",
                    "jev",
                    judge.model,
                    BOUNDARY_NOUL.id,
                    item.input_fingerprint if item else None,
                    item.failure_code if item else "missing_judgment",
                )
            )
    return tuple(result)
