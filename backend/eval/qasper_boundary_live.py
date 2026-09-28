"""Bounded Jev segmentation-purpose probe across eight public QASPER papers."""

import argparse
import asyncio
import getpass
import hashlib
import json
import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

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
from eval.qasper_judge_trial import USER_CALL_CAP, selected_cases
from eval.qasper_trial import load_papers

TRIAL_VERSION = "zenith-r1-qasper-boundary-8x4-v1"
PAPERS = 8
BOUNDARIES_PER_PAPER = 4
# Cumulative session ledger after the first QASPER boundary probe. A rerun
# must account for that probe as well as the earlier judge comparisons.
PRIOR_LIVE_CALLS = 930
MAX_CALLS = PAPERS * BOUNDARIES_PER_PAPER


class MeasuredJevJudge(JevJudge):
    last_batch: AssessmentBatch | None = None

    async def assess(self, question: str, candidates: list[Candidate]) -> AssessmentBatch:
        self.last_batch = await super().assess(question, candidates)
        return self.last_batch


async def run(dev_json: Path, key: str) -> dict[str, object]:
    if PRIOR_LIVE_CALLS + MAX_CALLS > USER_CALL_CAP:
        raise ValueError("boundary trial exceeds cumulative Jev call cap")
    selected = selected_cases(load_papers(dev_json))[:PAPERS]
    if len(selected) != PAPERS:
        raise ValueError("not enough QASPER papers")
    quota = JevQuota(MAX_CALLS, 500_000, max_concurrency=1)
    allowed: dict[object, str] = {}

    async def public_only(question: str, candidate: Candidate, purpose: Purpose) -> bool:
        return (
            purpose is Purpose.SEGMENTATION
            and question == BOUNDARY_QUESTION
            and allowed.get(candidate.id) == candidate.text
        )

    judge = MeasuredJevJudge(
        api_key=key,
        model="jev-1.13.0",
        formulation=Formulation.NOUL,
        rubric=BOUNDARY_NOUL,
        policy=ProcessingPolicy(segmentation=True),
        purpose=Purpose.SEGMENTATION,
        authorize=public_only,
        quota=quota,
        max_concurrency=1,
        deadline_seconds=60.0,
    )
    rows: list[dict[str, object]] = []
    input_tokens = 0
    output_tokens = 0
    try:
        for paper, _ in selected:
            source = Source(
                uuid5(NAMESPACE_URL, f"qasper-v0.3:{paper.id}"),
                None,
                paper.text,
                hashlib.sha256(paper.text.encode()).hexdigest(),
                "qasper-v0.3-render-v1",
                "qasper-v0.3",
            )
            spans = atomic_spans(source, max_chars=1000)
            structural = structural_groups(source, spans, target_chars=1200, max_chars=1600)
            pairs = {left.end: (left, right) for left, right in zip(spans, spans[1:], strict=False)}
            proposals = [pairs[group.end] for group in structural[:-1] if group.end in pairs]
            chosen = [
                proposals[min(len(proposals) - 1, index * len(proposals) // BOUNDARIES_PER_PAPER)]
                for index in range(min(BOUNDARIES_PER_PAPER, len(proposals)))
            ]
            if len(chosen) != BOUNDARIES_PER_PAPER:
                raise ValueError("frozen paper lacks four candidate boundaries")
            ids = frozenset(boundary_id(left, right) for left, right in chosen)
            allowed = {
                boundary_id(left, right): (
                    f"LEFT SOURCE CONTEXT:\n{source.text[max(0, left.end - 500) : left.end]}"
                    f"\n<BOUNDARY>\nRIGHT SOURCE CONTEXT:\n"
                    f"{source.text[right.start : min(len(source.text), right.start + 500)]}"
                )
                for left, right in chosen
            }
            assessments = await assess_boundaries(
                source,
                spans,
                judge,
                max_boundaries=BOUNDARIES_PER_PAPER,
                selected_ids=ids,
            )
            if judge.last_batch is not None:
                input_tokens += judge.last_batch.usage or 0
                output_tokens += judge.last_batch.output_tokens or 0
            jev_groups = structural_groups(
                source,
                spans,
                target_chars=1200,
                max_chars=1600,
                assessments=assessments,
            )
            rows.append(
                {
                    "paper_id": paper.id,
                    "selected_boundaries": len(ids),
                    "assessed": sum(
                        item.status == "assessed" for item in assessments if item.boundary_id in ids
                    ),
                    "failed_codes": [
                        item.failure_code
                        for item in assessments
                        if item.boundary_id in ids and item.status != "assessed"
                    ],
                    "structural_groups": len(structural),
                    "jev_groups": len(jev_groups),
                    "changed_group_ids": sum(
                        left.id != right.id
                        for left, right in zip(structural, jev_groups, strict=False)
                    )
                    + abs(len(structural) - len(jev_groups)),
                    "coverage_verified": "".join(item.text for item in jev_groups) == paper.text,
                    "selected_judgments": [
                        {
                            "boundary_id": str(item.boundary_id),
                            "status": item.status,
                            "probability": item.probability,
                            "failure_code": item.failure_code,
                        }
                        for item in assessments
                        if item.boundary_id in ids
                    ],
                }
            )
            print(f"  assessed public paper {len(rows)}/{PAPERS}", flush=True)
    finally:
        await judge.aclose()
    return {
        "trial_version": TRIAL_VERSION,
        "dataset_sha256": hashlib.sha256(dev_json.read_bytes()).hexdigest(),
        "selection": (
            "first 8 of frozen 40 hash-selected public QASPER papers; "
            "4 evenly spaced structural boundaries each"
        ),
        "rubric_id": BOUNDARY_NOUL.id,
        "rubric_hash": BOUNDARY_NOUL.hash,
        "model": "jev-1.13.0",
        "processing_purpose": Purpose.SEGMENTATION,
        "prior_live_calls_recorded": PRIOR_LIVE_CALLS,
        "jev_calls": quota.requests,
        "input_tokens_reserved_or_reconciled": quota.input_tokens,
        "provider_input_tokens_reported": input_tokens,
        "provider_output_tokens_reported": output_tokens,
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-json", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    key = os.environ.pop("ZENITH_JEV_API_KEY", None) or getpass.getpass(
        "Public QASPER boundary key (input hidden): "
    )
    result = asyncio.run(run(options.dev_json, key))
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {options.output}")


if __name__ == "__main__":
    main()
