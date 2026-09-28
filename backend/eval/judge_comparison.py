# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
"""Matched public-candidate TEI/Jev comparison. Never accepts private corpus paths."""

import argparse
import asyncio
import getpass
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import cast
from uuid import UUID, uuid5

from app.core.hardware import PROFILES
from app.features.retrieval.judging.jev import JevJudge, JevQuota, ProcessingPolicy, Purpose
from app.features.retrieval.judging.protocol import Candidate, Outcome
from app.features.retrieval.judging.rubrics import Formulation
from app.features.retrieval.reranker import TeiReranker

FIXTURE = Path(__file__).resolve().parent / "fixtures/evidence-v3-public-v1.json"
NAMESPACE = UUID("cb56ea1c-2d16-43cf-a03d-55671c04ca74")
MODEL = "jev-1.13.0"
# TypeSafe public list price checked on 2026-09-25. The adapter also reserves a
# conservative byte-count budget per call. Reconfirm this price before another live run.
USD_PER_INPUT_TOKEN = 42 / 1_000_000_000
USER_MAX_USD = 0.50
USER_MAX_CALLS = 1_000


def ndcg(grades: list[int], ordering: list[int]) -> float:
    def dcg(indices: list[int]) -> float:
        return sum(
            (2 ** grades[index] - 1) / math.log2(rank + 2) for rank, index in enumerate(indices)
        )

    ideal = dcg(sorted(range(len(grades)), key=lambda index: -grades[index]))
    return dcg(ordering) / ideal if ideal else 1.0


def metrics(grades: list[int], ordering: list[int]) -> dict[str, float | bool]:
    relevant = {index for index, grade in enumerate(grades) if grade >= 3}
    return {
        "ndcg_at_8": ndcg(grades, ordering),
        "best_at_1": grades[ordering[0]] == max(grades),
        "relevant_recall_at_2": len(relevant.intersection(ordering[:2])) / len(relevant)
        if relevant
        else 1.0,
    }


async def run(tei_url: str, live_jev: bool) -> dict[str, object]:
    raw = FIXTURE.read_bytes()
    fixture = json.loads(raw)
    candidates = [
        Candidate(uuid5(NAMESPACE, item["id"]), item["text"]) for item in fixture["candidates"]
    ]
    ids = [item["id"] for item in fixture["candidates"]]
    queries = fixture["queries"]
    calls = len(queries) * len(candidates) * 2 if live_jev else 0
    if calls > USER_MAX_CALLS:
        raise ValueError("fixture exceeds approved Jev call ceiling")
    # Even the quota's 4096-token overhead per call fits under the approved maximum.
    max_input_tokens = int(USER_MAX_USD / USD_PER_INPUT_TOKEN)
    estimated_floor = sum(
        len(query["text"].encode()) + sum(len(item.text.encode()) + 6000 for item in candidates)
        for query in queries
    ) * (2 if live_jev else 0)
    if estimated_floor > max_input_tokens:
        raise ValueError("public fixture exceeds approved spend ceiling")
    quota = JevQuota(USER_MAX_CALLS, max_input_tokens)

    async def public_only(question: str, candidate: Candidate, purpose: Purpose) -> bool:
        return purpose is Purpose.RERANKING and candidate.id in {item.id for item in candidates}

    key = os.environ.get("ZENITH_JEV_API_KEY")
    if live_jev and not key:
        if sys.stdin.isatty():
            key = getpass.getpass("Jev API key (input hidden; never written to disk): ")
        else:
            print("Waiting for Jev API key on stdin (not echoed or saved).", flush=True)
            key = sys.stdin.readline().strip()
        if not key:
            raise ValueError("missing Jev API key")
    judges = (
        {
            "jev_score6": JevJudge(
                api_key=key,
                model=MODEL,
                formulation=Formulation.SCORE6,
                policy=ProcessingPolicy(reranking=True),
                purpose=Purpose.RERANKING,
                authorize=public_only,
                quota=quota,
                deadline_seconds=60,
            ),
            "jev_noul": JevJudge(
                api_key=key,
                model=MODEL,
                formulation=Formulation.NOUL,
                policy=ProcessingPolicy(reranking=True),
                purpose=Purpose.RERANKING,
                authorize=public_only,
                quota=quota,
                deadline_seconds=60,
            ),
        }
        if live_jev
        else {}
    )
    tei = TeiReranker(url=tei_url, profile=PROFILES["cpu"])
    model = await tei.model_identity()
    rows: list[dict[str, object]] = []
    usage_input = 0
    usage_output = 0
    try:
        for query in queries:
            question = query["text"]
            grades = query["grades"]
            started = time.perf_counter()
            scored = await tei.rank(question, [item.text for item in candidates])
            tei_ms = int((time.perf_counter() - started) * 1000)
            tei_order = [item.index for item in scored]
            row: dict[str, object] = {
                "query_id": query["id"],
                "tei": {
                    "order": [ids[index] for index in tei_order],
                    "scores": [item.score for item in scored],
                    "elapsed_ms": tei_ms,
                    "metrics": metrics(grades, tei_order),
                },
            }
            for name, judge in judges.items():
                started = time.perf_counter()
                batch = await judge.assess(question, candidates)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                if any(item.outcome is not Outcome.ASSESSED for item in batch.judgments):
                    raise RuntimeError(
                        f"{name} incomplete on {query['id']}: "
                        f"{[item.failure_code for item in batch.judgments if item.failure_code]}"
                    )
                by_id = {item.candidate_id: item for item in batch.judgments}
                score_values: list[float] = []
                for candidate in candidates:
                    value = by_id[candidate.id].rank_value
                    assert value is not None
                    score_values.append(value)

                order = sorted(
                    range(len(candidates)),
                    key=lambda index: -score_values[index],
                )
                row[name] = {
                    "order": [ids[index] for index in order],
                    "scores": [by_id[candidates[index].id].rank_value for index in order],
                    "elapsed_ms": elapsed_ms,
                    "metrics": metrics(grades, order),
                    "rubric_id": batch.judgments[0].rubric_id,
                    "rubric_hash": batch.judgments[0].rubric_hash,
                    "utility_map_id": batch.judgments[0].utility_map_id,
                    "reported_model": batch.reported_model,
                    "input_tokens": batch.usage,
                    "output_tokens": batch.output_tokens,
                }
                usage_input += batch.usage or 0
                usage_output += batch.output_tokens or 0
            rows.append(row)
    finally:
        for judge in judges.values():
            await judge.aclose()
    report: dict[str, object] = {
        "fixture_id": fixture["id"],
        "fixture_sha256": hashlib.sha256(raw).hexdigest(),
        "label_provenance": fixture["label_provenance"],
        "candidate_ids": ids,
        "query_ids": [query["id"] for query in queries],
        "tei_endpoint": tei_url,
        "tei_reported_model": model,
        "jev_requested_model": MODEL if live_jev else None,
        "jev_calls": quota.requests,
        "jev_input_tokens_reported": usage_input if live_jev else None,
        "jev_output_tokens_reported": usage_output if live_jev else None,
        "jev_input_tokens_reserved_or_reconciled": quota.input_tokens if live_jev else None,
        "jev_estimated_usd_from_reported_usage": usage_input * USD_PER_INPUT_TOKEN
        if live_jev
        else None,
        "jev_conservative_budget_usd": quota.input_tokens * USD_PER_INPUT_TOKEN
        if live_jev
        else None,
        "approved_caps": {"usd": USER_MAX_USD, "calls": USER_MAX_CALLS},
        "rows": rows,
    }
    for name in ("tei", "jev_score6", "jev_noul"):
        if name == "tei" or live_jev:
            data = [cast(dict[str, object], row[name]) for row in rows]
            metric_rows = [cast(dict[str, float | bool], item["metrics"]) for item in data]
            elapsed = sorted(cast(int, item["elapsed_ms"]) for item in data)
            report[f"{name}_summary"] = {
                "mean_ndcg_at_8": sum(float(item["ndcg_at_8"]) for item in metric_rows) / len(data),
                "best_at_1_count": sum(bool(item["best_at_1"]) for item in metric_rows),
                "mean_relevant_recall_at_2": sum(
                    float(item["relevant_recall_at_2"]) for item in metric_rows
                )
                / len(data),
                "median_elapsed_ms": elapsed[len(data) // 2],
            }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tei-url", default="http://127.0.0.1:18082")
    parser.add_argument("--live-jev", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(run(args.tei_url, args.live_jev))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key.endswith("_summary")
                or key
                in {
                    "fixture_sha256",
                    "jev_calls",
                    "jev_estimated_usd_from_reported_usage",
                    "jev_conservative_budget_usd",
                }
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
