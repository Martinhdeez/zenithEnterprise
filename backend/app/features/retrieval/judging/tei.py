"""Compatibility adapter for the installed local TEI cross-encoder."""

import asyncio
import hashlib
import time

from app.features.retrieval.judging.protocol import (
    AssessmentBatch,
    Candidate,
    Judge,
    JudgeCapabilities,
    Judgment,
    Outcome,
    ScoreKind,
)
from app.features.retrieval.reranker import RERANK_TIMEOUT, Ranker, RerankerUnavailable, TeiReranker


class TeiJudge(Judge):
    capabilities = JudgeCapabilities(reported_model_identity=True)

    def __init__(self, reranker: Ranker, *, truncate: bool | None = None) -> None:
        self.reranker = reranker
        self.truncate = truncate

    async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
        started = time.perf_counter()
        if len({item.id for item in candidates}) != len(candidates):
            raise ValueError("duplicate candidate identity")
        # Reuse scores for identical rendered text within this request. Source IDs remain
        # separate judgments; equal scores retain incoming order at the service boundary.
        unique_texts = list(dict.fromkeys(item.text for item in candidates))
        try:
            async with asyncio.timeout(RERANK_TIMEOUT):
                scored = (
                    await self.reranker.rank(question, unique_texts, truncate=self.truncate)
                    if isinstance(self.reranker, TeiReranker) and self.truncate is not None
                    else await self.reranker.rank(question, unique_texts)
                )
                model = (
                    await self.reranker.model_identity()
                    if candidates and isinstance(self.reranker, TeiReranker)
                    else None
                )
        except TimeoutError as exc:
            raise RerankerUnavailable("reranker total deadline exceeded") from exc
        values = {unique_texts[item.index]: item.score for item in scored}
        provider = "tei" if isinstance(self.reranker, TeiReranker) else "local-eval-ranker"
        fingerprint = (
            hashlib.sha256(f"{self.reranker.url}\0{model}".encode()).hexdigest()
            if model and isinstance(self.reranker, TeiReranker)
            else None
        )
        judgments = tuple(
            Judgment(
                candidate_id=item.id,
                outcome=Outcome.ASSESSED,
                rank_value=values[item.text],
                score_kind=ScoreKind.RAW_RANK_SCORE,
                provider=provider,
                reported_model=model,
                deployment_fingerprint=fingerprint,
                input_fingerprint=hashlib.sha256(f"{question}\0{item.text}".encode()).hexdigest(),
            )
            for item in candidates
        )
        return AssessmentBatch(
            requested_ids=tuple(item.id for item in candidates),
            judgments=judgments,
            provider=provider,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            reported_model=model,
            provider_fingerprint=fingerprint,
        )
