"""Deterministic, explicit test judge; never evidence of model quality."""

from time import perf_counter

from app.features.retrieval.judging.protocol import (
    AssessmentBatch,
    Candidate,
    Judge,
    JudgeCapabilities,
    Judgment,
    Outcome,
    ScoreKind,
)


class FakeJudge(Judge):
    capabilities = JudgeCapabilities(reported_model_identity=True)

    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores

    async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
        started = perf_counter()
        judgments = tuple(
            Judgment(
                candidate_id=item.id,
                outcome=Outcome.ASSESSED,
                rank_value=self.scores[item.text],
                score_kind=ScoreKind.RAW_RANK_SCORE,
                provider="test-fake",
                reported_model="deterministic-test-fake",
            )
            for item in candidates
        )
        return AssessmentBatch(
            requested_ids=tuple(item.id for item in candidates),
            judgments=judgments,
            provider="test-fake",
            elapsed_ms=int((perf_counter() - started) * 1000),
            reported_model="deterministic-test-fake",
        )
