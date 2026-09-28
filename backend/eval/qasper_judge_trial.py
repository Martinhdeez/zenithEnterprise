"""Matched public QASPER shortlist comparison: dense, local TEI, Jev Noul/Score.

Only 40 hash-selected, distinct public development papers are eligible. The
same eight locally selected candidates and BGE tokenizer budget feed every
judge. The output contains IDs and scores, never source paragraphs or a key.
"""

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import time
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from app.features.retrieval.judging.jev import JevJudge, JevQuota, ProcessingPolicy, Purpose
from app.features.retrieval.judging.protocol import Candidate, Outcome
from app.features.retrieval.judging.rubrics import Formulation
from eval.embedder import TeiEmbedder
from eval.lossless_trial import TrialUnit, rank, rendered_token_counts
from eval.qasper_trial import Case, Paper, load_papers, score_case, units_for_paper

TRIAL_VERSION = "zenith-qasper-dev-40-fixed-candidates-v1"
SAMPLE_PAPERS = 40
# The earlier report records 186 at dispatch time. This lower bound includes
# that trial, both diagnostics, and the later QASPER boundary probe. Increase
# it after every new live run; never reuse the historical report's 186 here.
PRIOR_LIVE_CALLS = 930
USER_CALL_CAP = 1000
USER_USD_CAP = 0.50
INPUT_USD_PER_TOKEN = 42 / 1_000_000_000
TRIAL_RESERVED_INPUT_TOKENS = 5_000_000
MODEL = "jev-1.13.0"


def selected_cases(papers: tuple[Paper, ...]) -> tuple[tuple[Paper, Case], ...]:
    eligible = [paper for paper in papers if paper.cases]
    chosen = sorted(eligible, key=lambda paper: hashlib.sha256(paper.id.encode()).hexdigest())[
        :SAMPLE_PAPERS
    ]
    return tuple(
        (
            paper,
            min(paper.cases, key=lambda case: hashlib.sha256(case.id.encode()).hexdigest()),
        )
        for paper in chosen
    )


async def run(
    dev_json: Path,
    *,
    embed_url: str,
    rerank_url: str,
    key: str | None,
    preflight_only: bool = False,
) -> dict[str, object]:
    from app.core.hardware import PROFILES
    from app.features.retrieval.reranker import TeiReranker

    selected = selected_cases(load_papers(dev_json))
    if len(selected) != SAMPLE_PAPERS:
        raise ValueError("the frozen QASPER split does not have enough eligible papers")
    embedder = TeiEmbedder(embed_url)
    tei = TeiReranker(url=rerank_url, profile=PROFILES["gpu"])
    requests = 0
    candidate_rows: list[tuple[Paper, Case, list[Candidate], dict[str, int], list[TrialUnit]]] = []
    # This local-only preparation fixes every candidate before any external dispatch.
    for paper, case in selected:
        legacy, _ = units_for_paper(paper)
        vectors = embedder.encode([unit.text for unit in legacy], batch=4)
        query_vector = embedder.encode([case.question], batch=1)[0]
        top = rank(legacy, vectors, query_vector)[:8]
        candidates = [
            Candidate(uuid5(NAMESPACE_URL, f"qasper-v0.3:{paper.id}:{unit.id}"), unit.text)
            for unit in top
        ]
        counts = rendered_token_counts(top, embed_url)
        candidate_rows.append((paper, case, candidates, counts, top))
        requests += 2 * len(candidates)
    if PRIOR_LIVE_CALLS + requests > USER_CALL_CAP:
        raise ValueError("QASPER sample would exceed the cumulative approved Jev call cap")
    conservative_input_tokens = sum(
        2 * sum(len(case.question.encode()) + len(item.text.encode()) + 6000 for item in items)
        for _, case, items, _, _ in candidate_rows
    )
    if conservative_input_tokens > TRIAL_RESERVED_INPUT_TOKENS:
        raise ValueError("QASPER sample would exceed the approved trial token reservation")
    if TRIAL_RESERVED_INPUT_TOKENS * INPUT_USD_PER_TOKEN >= USER_USD_CAP:
        raise ValueError("trial reservation exceeds approved spend cap")
    if preflight_only:
        return {
            "trial_version": TRIAL_VERSION,
            "selected_papers": [paper.id for paper, _ in selected],
            "selected_question_ids": [case.id for _, case in selected],
            "planned_jev_calls": requests,
            "conservative_input_token_preflight": conservative_input_tokens,
            "external_calls": 0,
        }
    if not key:
        raise ValueError("live Jev trial needs a key")
    # The quota includes retries. It is shared across Score and Noul.
    quota = JevQuota(requests, TRIAL_RESERVED_INPUT_TOKENS, max_concurrency=1)
    active_question: str | None = None
    active_candidates: dict[object, str] = {}

    async def public_only(question: str, candidate: Candidate, purpose: Purpose) -> bool:
        return (
            purpose is Purpose.RERANKING
            and question == active_question
            and active_candidates.get(candidate.id) == candidate.text
        )

    judges = {
        "jev_noul": JevJudge(
            api_key=key,
            model=MODEL,
            formulation=Formulation.NOUL,
            policy=ProcessingPolicy(reranking=True),
            purpose=Purpose.RERANKING,
            authorize=public_only,
            quota=quota,
            max_concurrency=1,
            deadline_seconds=60.0,
        ),
        "jev_score6": JevJudge(
            api_key=key,
            model=MODEL,
            formulation=Formulation.SCORE6,
            policy=ProcessingPolicy(reranking=True),
            purpose=Purpose.RERANKING,
            authorize=public_only,
            quota=quota,
            max_concurrency=1,
            deadline_seconds=60.0,
        ),
    }
    rows: list[dict[str, object]] = []
    usage_input = 0
    usage_output = 0
    try:
        for paper, case, candidates, counts, ranked in candidate_rows:
            # The frozen candidate order is dense similarity. Keep it for ties.
            active_question = case.question
            active_candidates = {candidate.id: candidate.text for candidate in candidates}
            routes: dict[str, object] = {"dense": score_case(case, ranked, counts)}
            start = time.perf_counter()
            tei_scores = await tei.rank(case.question, [item.text for item in candidates])
            routes["main_tei"] = {
                **score_case(case, [ranked[item.index] for item in tei_scores], counts),
                "elapsed_ms": round((time.perf_counter() - start) * 1000),
                "raw_scores": [item.score for item in tei_scores],
            }
            for name, judge in judges.items():
                start = time.perf_counter()
                batch = await judge.assess(case.question, candidates)
                usage_input += batch.usage or 0
                usage_output += batch.output_tokens or 0
                by_id = {item.candidate_id: item for item in batch.judgments}
                if any(item.outcome is not Outcome.ASSESSED for item in batch.judgments):
                    routes[name] = {
                        "status": "unavailable",
                        "failure_codes": [
                            item.failure_code
                            for item in batch.judgments
                            if item.outcome is not Outcome.ASSESSED
                        ],
                    }
                    continue
                order = sorted(
                    range(len(candidates)),
                    key=lambda index: -cast(float, by_id[candidates[index].id].rank_value),
                )
                first = batch.judgments[0]
                routes[name] = {
                    **score_case(case, [ranked[index] for index in order], counts),
                    "status": "assessed",
                    "elapsed_ms": round((time.perf_counter() - start) * 1000),
                    "score_kind": first.score_kind,
                    "rubric_id": first.rubric_id,
                    "rubric_hash": first.rubric_hash,
                    "utility_map_id": first.utility_map_id,
                    "reported_model": batch.reported_model,
                    "input_tokens": batch.usage,
                    "output_tokens": batch.output_tokens,
                    "ranks": [by_id[candidates[index].id].rank_value for index in order],
                    "input_fingerprints": [
                        by_id[candidates[index].id].input_fingerprint for index in order
                    ],
                    "grade_distributions": [
                        by_id[candidates[index].id].grade_distribution for index in order
                    ],
                }
            rows.append(
                {
                    "paper_id": paper.id,
                    "question_id": case.id,
                    "candidate_count": len(candidates),
                    "candidate_ids": [str(item.id) for item in candidates],
                    "routes": routes,
                }
            )
            print(f"  assessed public question {len(rows)}/{len(selected)}", flush=True)
    finally:
        for judge in judges.values():
            await judge.aclose()
    summaries: dict[str, dict[str, float | int]] = {}
    for name in ("dense", "main_tei", "jev_noul", "jev_score6"):
        scores = [
            cast(dict[str, object], cast(dict[str, object], row["routes"])[name]) for row in rows
        ]
        assessed = [score for score in scores if score.get("status") != "unavailable"]
        summaries[name] = {
            "assessed_cases": len(assessed),
            "unavailable_cases": len(scores) - len(assessed),
            "complete_evidence_rate": sum(bool(score["complete_evidence"]) for score in assessed)
            / len(assessed)
            if assessed
            else 0.0,
            "mean_evidence_recall": sum(cast(float, score["evidence_recall"]) for score in assessed)
            / len(assessed)
            if assessed
            else 0.0,
        }
    return {
        "trial_version": TRIAL_VERSION,
        "dataset_sha256": hashlib.sha256(dev_json.read_bytes()).hexdigest(),
        "selected_papers": [paper.id for paper, _ in selected],
        "selection": "first hash-ranked eligible question in 40 hash-ranked distinct dev papers",
        "label_provenance": "QASPER practitioner evidence paragraphs, exact unique text",
        "candidate_policy": "same dense top-8 legacy chunks for every judge",
        "embedding_endpoint": embed_url,
        "tei_reranker_endpoint": rerank_url,
        "tei_reported_model": await tei.model_identity(),
        "jev_requested_model": MODEL,
        "jev_calls": quota.requests,
        "jev_conservative_input_token_preflight": conservative_input_tokens,
        "jev_input_tokens_reserved_or_reconciled": quota.input_tokens,
        "jev_input_tokens_reported": usage_input,
        "jev_output_tokens_reported": usage_output,
        "jev_estimated_input_usd": usage_input * INPUT_USD_PER_TOKEN,
        "approved_cumulative_caps": {"calls": USER_CALL_CAP, "usd": USER_USD_CAP},
        "prior_live_calls_recorded": PRIOR_LIVE_CALLS,
        "summaries": summaries,
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-json", type=Path, required=True)
    parser.add_argument("--embed-url", default="http://127.0.0.1:18081")
    parser.add_argument("--rerank-url", default="http://127.0.0.1:18083")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    options = parser.parse_args()
    key = None
    if not options.preflight_only:
        key = os.environ.pop("ZENITH_JEV_API_KEY", None) or getpass.getpass(
            "Public QASPER Jev key (input hidden): "
        )
    result = asyncio.run(
        run(
            options.dev_json,
            embed_url=options.embed_url,
            rerank_url=options.rerank_url,
            key=key,
            preflight_only=options.preflight_only,
        )
    )
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {options.output}")


if __name__ == "__main__":
    main()
