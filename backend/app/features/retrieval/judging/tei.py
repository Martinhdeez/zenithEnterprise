"""Associate existing local TEI scores with their original source identities."""

from time import perf_counter

from app.features.retrieval.judging.protocol import (
    AssessmentBatch,
    Candidate,
    Judgment,
    Outcome,
)
from app.features.retrieval.reranker import TeiReranker


class TeiJudge:
    def __init__(self, reranker: TeiReranker) -> None:
        self.reranker = reranker

    async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
        started = perf_counter()
        if len({item.id for item in candidates}) != len(candidates):
            raise ValueError("duplicate candidate identity")
        # Preserve the legacy input, including repeated text: distinct sources remain
        # distinct candidates and batching/order are unchanged.
        scored = await self.reranker.rank(question, [item.text for item in candidates])
        return AssessmentBatch(
            requested_ids=tuple(item.id for item in candidates),
            judgments=tuple(
                Judgment(candidates[item.index].id, Outcome.ASSESSED, item.score) for item in scored
            ),
            provider="tei",
            elapsed_ms=int((perf_counter() - started) * 1000),
        )
