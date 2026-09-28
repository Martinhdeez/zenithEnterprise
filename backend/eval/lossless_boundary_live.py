"""Explicit public-only Jev boundary trial; prompts for a key without storing it."""

import argparse
import asyncio
import getpass
import hashlib
import json
import os
from pathlib import Path

from app.features.ingestion.chunking.boundary import BOUNDARY_QUESTION, assess_boundaries
from app.features.ingestion.chunking.lossless import (
    Source,
    atomic_spans,
    boundary_id,
    structural_groups,
)
from app.features.retrieval.judging.jev import JevJudge, JevQuota, ProcessingPolicy, Purpose
from app.features.retrieval.judging.protocol import AssessmentBatch, Candidate
from app.features.retrieval.judging.rubrics import BOUNDARY_NOUL, Formulation
from eval.lossless_trial import EMBED_MODEL, SNAPSHOT_SHA256, SOURCE_ID, SOURCE_URL

MAX_LIVE_BOUNDARIES = 8


class MeasuredJevJudge(JevJudge):
    last_batch: AssessmentBatch | None = None

    async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
        self.last_batch = await super().assess(question, candidates)
        return self.last_batch


async def run(snapshot: Path, key: str) -> dict[str, object]:
    text = snapshot.read_text(encoding="utf-8")
    if hashlib.sha256(text.encode()).hexdigest() != SNAPSHOT_SHA256:
        raise ValueError("public snapshot changed")
    source = Source(
        SOURCE_ID, None, text, SNAPSHOT_SHA256, "zenith-eval-html-main-v1", "2026-09-26"
    )
    spans = atomic_spans(source, max_chars=1000)
    baseline = structural_groups(source, spans, target_chars=1200, max_chars=1600)
    boundaries = {
        left.end: boundary_id(left, right) for left, right in zip(spans, spans[1:], strict=False)
    }
    proposed = [boundaries[group.end] for group in baseline[:-1] if group.end in boundaries]
    # Frozen, evenly spaced candidate positions. No model output chooses its own trial set.
    selected = frozenset(
        proposed[min(len(proposed) - 1, index * len(proposed) // MAX_LIVE_BOUNDARIES)]
        for index in range(min(MAX_LIVE_BOUNDARIES, len(proposed)))
    )

    async def authorize(question: str, candidate: Candidate, purpose: Purpose) -> bool:
        if purpose is not Purpose.SEGMENTATION or question != BOUNDARY_QUESTION:
            return False
        parts = candidate.text.split("\n<BOUNDARY>\nRIGHT SOURCE CONTEXT:\n", maxsplit=1)
        return (
            len(parts) == 2
            and parts[0].startswith("LEFT SOURCE CONTEXT:\n")
            and parts[0].removeprefix("LEFT SOURCE CONTEXT:\n") in text
            and parts[1] in text
        )

    judge = MeasuredJevJudge(
        api_key=key,
        model="jev-1.13.0",
        formulation=Formulation.NOUL,
        rubric=BOUNDARY_NOUL,
        policy=ProcessingPolicy(segmentation=True),
        purpose=Purpose.SEGMENTATION,
        authorize=authorize,
        quota=JevQuota(MAX_LIVE_BOUNDARIES, 150_000, max_concurrency=1),
        max_concurrency=1,
        deadline_seconds=90.0,
    )
    try:
        assessments = await assess_boundaries(
            source,
            spans,
            judge,
            max_boundaries=MAX_LIVE_BOUNDARIES,
            selected_ids=selected,
        )
    finally:
        await judge.aclose()
    grouped = structural_groups(
        source,
        spans,
        target_chars=1200,
        max_chars=1600,
        assessments=assessments,
    )
    return {
        "trial": "zenith-r1-epa-boundary-v1",
        "source_url": SOURCE_URL,
        "source_sha256": SNAPSHOT_SHA256,
        "rubric_id": BOUNDARY_NOUL.id,
        "rubric_hash": BOUNDARY_NOUL.hash,
        "model": "jev-1.13.0",
        "embedding_model_for_later_matrix": EMBED_MODEL,
        "processing_purpose": Purpose.SEGMENTATION,
        "selected_boundaries": len(selected),
        "requests_reserved": judge.quota.requests,
        "provider_input_tokens": judge.last_batch.usage if judge.last_batch else None,
        "provider_output_tokens": judge.last_batch.output_tokens if judge.last_batch else None,
        "assessed": sum(item.status == "assessed" for item in assessments),
        "failed_or_unavailable": [
            {"boundary_id": str(item.boundary_id), "code": item.failure_code}
            for item in assessments
            if item.boundary_id in selected and item.status != "assessed"
        ],
        "judgments": [
            {
                "boundary_id": str(item.boundary_id),
                "probability": item.probability,
                "status": item.status,
                "model": item.model,
                "rubric_id": item.rubric_id,
                "input_fingerprint": item.input_fingerprint,
            }
            for item in assessments
            if item.boundary_id in selected
        ],
        "structural_group_count": len(baseline),
        "jev_preferred_group_count": len(grouped),
        "changed_group_ids": sum(
            left.id != right.id for left, right in zip(baseline, grouped, strict=False)
        ),
        "source_coverage_verified": "".join(item.text for item in grouped) == text,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    key = os.environ.pop("ZENITH_JEV_API_KEY", None) or getpass.getpass(
        "Public-only Jev segmentation key (input hidden): "
    )
    result = asyncio.run(run(options.snapshot, key))
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {options.output}")


if __name__ == "__main__":
    main()
