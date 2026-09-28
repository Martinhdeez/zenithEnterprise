"""Frozen QASPER test-split TEI/Jev Noul comparison with public-only egress.

Prepare without reading answer annotations, run against the frozen manifest,
then score against the human labels. Text-bearing manifests and raw responses
belong in the ignored local trial directory, never in the public Git tree.
"""

import argparse
import asyncio
import csv
import getpass
import hashlib
import json
import math
import os
import random
import statistics
import time
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

from app.core.hardware import PROFILES
from app.features.ingestion.chunking.chunker import chunk_stream
from app.features.retrieval.judging.jev import JevJudge, JevQuota, ProcessingPolicy, Purpose
from app.features.retrieval.judging.protocol import Candidate, Outcome, ScoreKind
from app.features.retrieval.judging.rubrics import NOUL, Formulation
from app.features.retrieval.reranker import TeiReranker
from eval.embedder import TeiEmbedder
from eval.evidence_common import TrialUnit, displayed_range, rank, rendered_token_counts
from eval.qasper_common import Case, Paper, build_paper, score_case

TEST_SHA256 = "6e29ad410e6e39aa1936017fb965b30a20eb2e7751997f55b97c9d281aa884e5"
ARCHIVE_SHA256 = "72a52a41193e2838b8074f80ac074b94f956b84886c36a61c58a7df4171bdd72"
TRIAL_ID = "zenith-qasper-test-240papers-fixed8-noul-v1"
MANIFEST_SHA256 = "083ef6880efaac1307c6bedd3ab3b29d3038132370780cf54bce1610aa2a26c7"
PAPERS = 240
TOP_K = 8
PRIOR_CALLS = 999
ADDITIONAL_CALL_CAP = 10_000
ADDITIONAL_USD_CAP = 5.0
INPUT_USD_PER_TOKEN = 42 / 1_000_000_000
RESERVED_INPUT_TOKENS = 20_000_000
MODEL = "jev-1.13.0"
BOOTSTRAP_SEED = 1729
BOOTSTRAP_SAMPLES = 2000


def _test_json(path: Path) -> dict[str, dict[str, Any]]:
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != TEST_SHA256:
        raise ValueError("QASPER test archive is not the frozen v0.3 snapshot")
    return cast(dict[str, dict[str, Any]], json.loads(data))


def _unlabeled_paper(paper_id: str, source: dict[str, Any]) -> Paper:
    # The projection removes every answer before build_paper runs. Preparation
    # can select only by public paper/question identifiers and source text.
    qas: list[dict[str, Any]] = [
        {"question_id": qa["question_id"], "question": qa["question"], "answers": []}
        for qa in source["qas"]
    ]
    paper = build_paper(paper_id, {**source, "qas": qas})
    return paper


def _legacy_units(paper: Paper) -> list[TrialUnit]:
    units: list[TrialUnit] = []
    for index, item in enumerate(chunk_stream(paper.text)):
        start, end = displayed_range(paper.text, item.char_start, item.char_end)
        if paper.text[start:end] != item.text:
            raise ValueError("legacy source coordinate mismatch")
        units.append(TrialUnit(f"legacy:{index}", start, end, item.text))
    return units


def _save(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


async def prepare(path: Path, embed_url: str, rerank_url: str, output: Path) -> dict[str, object]:
    raw = _test_json(path)
    embedder = TeiEmbedder(embed_url)
    tei = TeiReranker(url=rerank_url, profile=PROFILES["gpu"])
    rows: list[dict[str, object]] = []
    ordered = sorted(raw, key=lambda identity: hashlib.sha256(identity.encode()).hexdigest())
    for paper_id in ordered:
        source = raw[paper_id]
        qas = cast(list[dict[str, Any]], source["qas"])
        if not qas:
            continue
        paper = _unlabeled_paper(paper_id, source)
        units = _legacy_units(paper)
        if not units:
            continue
        qa = min(
            qas, key=lambda item: hashlib.sha256(str(item["question_id"]).encode()).hexdigest()
        )
        question = str(qa["question"])
        vectors = embedder.encode([unit.text for unit in units], batch=4)
        query_vector = embedder.encode([question], batch=1)[0]
        ranked = rank(units, vectors, query_vector)[:TOP_K]
        counts = rendered_token_counts(ranked, embed_url)
        started = time.perf_counter()
        tei_scores = await tei.rank(question, [item.text for item in ranked])
        tei_order = [item.index for item in tei_scores]
        rows.append(
            {
                "paper_id": paper_id,
                "question_id": str(qa["question_id"]),
                "question": question,
                "source_sha256": hashlib.sha256(paper.text.encode()).hexdigest(),
                "candidates": [
                    {
                        "id": str(uuid5(NAMESPACE_URL, f"qasper-test-v0.3:{paper_id}:{unit.id}")),
                        "unit_id": unit.id,
                        "start": unit.start,
                        "end": unit.end,
                        "text": unit.text,
                        "rendered_tokens": counts[unit.id],
                    }
                    for unit in ranked
                ],
                "tei_order": tei_order,
                "tei_scores": [item.score for item in tei_scores],
                "tei_elapsed_ms": round((time.perf_counter() - started) * 1000),
            }
        )
        print(f"prepared public paper {len(rows)}/{PAPERS}", flush=True)
        if len(rows) == PAPERS:
            break
    if len(rows) != PAPERS:
        raise ValueError("not enough source-bearing test papers")
    planned_calls = sum(len(cast(list[object], row["candidates"])) for row in rows)
    estimated_tokens = sum(
        len(str(row["question"]).encode()) + len(str(candidate["text"]).encode()) + 6000
        for row in rows
        for candidate in cast(list[dict[str, object]], row["candidates"])
    )
    if planned_calls > ADDITIONAL_CALL_CAP or estimated_tokens > RESERVED_INPUT_TOKENS:
        raise ValueError("frozen trial exceeds additional call/token reservation")
    if RESERVED_INPUT_TOKENS * INPUT_USD_PER_TOKEN >= ADDITIONAL_USD_CAP:
        raise ValueError("frozen trial token reservation exceeds authorized dollars")
    manifest: dict[str, object] = {
        "trial_id": TRIAL_ID,
        "dataset_sha256": TEST_SHA256,
        "archive_sha256": ARCHIVE_SHA256,
        "selection": "first 240 SHA-ranked source-bearing test papers; lowest SHA-ranked "
        "question per paper, without reading answers",
        "candidate_policy": "BGE-M3 dense top eight of current-main chunks; same frozen "
        "candidates for TEI and Jev",
        "tei_model": await tei.model_identity(),
        "jev_model": MODEL,
        "jev_rubric_id": NOUL.id,
        "jev_rubric_hash": NOUL.hash,
        "planned_calls": planned_calls,
        "conservative_input_tokens": estimated_tokens,
        "approved_additional_calls": ADDITIONAL_CALL_CAP,
        "approved_additional_usd": ADDITIONAL_USD_CAP,
        "rows": rows,
    }
    _save(output, manifest)
    return manifest


async def run(manifest_path: Path, ledger_path: Path, key: str) -> dict[str, object]:
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != MANIFEST_SHA256:
        raise ValueError("held-out manifest differs from the frozen candidate snapshot")
    manifest = cast(dict[str, object], json.loads(manifest_path.read_text(encoding="utf-8")))
    if manifest.get("trial_id") != TRIAL_ID or manifest.get("dataset_sha256") != TEST_SHA256:
        raise ValueError("manifest identity does not match frozen held-out trial")
    manifest_rows = cast(list[dict[str, object]], manifest["rows"])
    if ledger_path.exists():
        ledger = cast(dict[str, object], json.loads(ledger_path.read_text(encoding="utf-8")))
        if (
            ledger.get("trial_id") != TRIAL_ID
            or ledger.get("manifest_sha256")
            != hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        ):
            raise ValueError("ledger belongs to a different manifest")
    else:
        ledger: dict[str, object] = {
            "trial_id": TRIAL_ID,
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "reserved": [],
            "rows": [],
        }
    reserved = cast(list[str], ledger["reserved"])
    if len(set(reserved)) != len(reserved):
        raise ValueError("duplicate reservation")
    allowed: dict[object, str] = {}
    active_question = ""

    async def public_only(question: str, candidate: Candidate, purpose: Purpose) -> bool:
        return (
            purpose is Purpose.RERANKING
            and question == active_question
            and allowed.get(candidate.id) == candidate.text
        )

    judge = JevJudge(
        api_key=key,
        model=MODEL,
        formulation=Formulation.NOUL,
        rubric=NOUL,
        policy=ProcessingPolicy(reranking=True),
        purpose=Purpose.RERANKING,
        authorize=public_only,
        quota=JevQuota(ADDITIONAL_CALL_CAP, RESERVED_INPUT_TOKENS, max_concurrency=1),
        max_concurrency=1,
        deadline_seconds=90.0,
    )
    try:
        for index, row in enumerate(manifest_rows, 1):
            if row["question_id"] in reserved:
                continue  # Unknown partial dispatch is never replayed for free.
            candidates = [
                Candidate(
                    uuid5(NAMESPACE_URL, f"qasper-test-v0.3:{row['paper_id']}:{item['unit_id']}"),
                    str(item["text"]),
                )
                for item in cast(list[dict[str, object]], row["candidates"])
            ]
            if len(reserved) * TOP_K + len(candidates) > ADDITIONAL_CALL_CAP:
                raise ValueError("additional Jev call cap exhausted")
            reserved.append(str(row["question_id"]))
            _save(ledger_path, ledger)  # Reserve every candidate before dispatch.
            active_question = str(row["question"])
            allowed = {candidate.id: candidate.text for candidate in candidates}
            started = time.perf_counter()
            batch = await judge.assess(active_question, candidates)
            by_id = {item.candidate_id: item for item in batch.judgments}
            valid = (
                batch.requested_ids == tuple(candidate.id for candidate in candidates)
                and len(by_id) == len(candidates)
                and all(
                    candidate.id in by_id
                    and (item := by_id[candidate.id]).outcome is Outcome.ASSESSED
                    and item.score_kind is ScoreKind.MODEL_PROBABILITY
                    and item.rank_value is not None
                    and item.rubric_id == NOUL.id
                    and item.rubric_hash == NOUL.hash
                    for candidate in candidates
                )
            )
            order = (
                sorted(
                    range(len(candidates)),
                    key=lambda i: -cast(float, by_id[candidates[i].id].rank_value),
                )
                if valid
                else None
            )
            cast(list[dict[str, object]], ledger["rows"]).append(
                {
                    "paper_id": row["paper_id"],
                    "question_id": row["question_id"],
                    "status": "assessed" if valid else "unavailable",
                    "order": order,
                    "scores": [
                        by_id[candidate.id].rank_value if valid else None
                        for candidate in candidates
                    ],
                    "failure_codes": [
                        item.failure_code
                        for item in batch.judgments
                        if item.outcome is not Outcome.ASSESSED
                    ],
                    "reported_model": batch.reported_model,
                    "input_tokens": batch.usage,
                    "output_tokens": batch.output_tokens,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000),
                }
            )
            _save(ledger_path, ledger)
            print(f"assessed public paper {index}/{len(manifest_rows)}", flush=True)
    finally:
        await judge.aclose()
    return ledger


def _interval(differences: list[float]) -> tuple[float, float]:
    rng = random.Random(BOOTSTRAP_SEED)
    draws = sorted(
        statistics.mean(rng.choice(differences) for _ in differences)
        for _ in range(BOOTSTRAP_SAMPLES)
    )
    return draws[49], draws[1949]


def _ranking_metrics(case: Case, ranked: list[TrialUnit]) -> dict[str, float | bool | None]:
    """Binary overlap with accepted evidence; exclude zero-hit cases from nDCG."""
    relevant = [
        any(
            unit.start < end and unit.end > start
            for option in case.evidence_options
            for start, end in option
        )
        for unit in ranked
    ]
    hits = sum(relevant)
    if not hits:
        return {"candidate_hit": False, "ndcg_at_8": None, "mrr_at_8": None}
    dcg = sum(float(hit) / math.log2(index + 2) for index, hit in enumerate(relevant))
    ideal = sum(1 / math.log2(index + 2) for index in range(hits))
    return {
        "candidate_hit": True,
        "ndcg_at_8": dcg / ideal,
        "mrr_at_8": 1 / (relevant.index(True) + 1),
    }


def _measured(row: dict[str, object], key: str) -> float:
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"missing numeric {key}")
    return float(value)


def _fallback_ndcg(row: dict[str, object]) -> float:
    key = "jev_ndcg_at_8" if row["jev_ndcg_at_8"] is not None else "tei_ndcg_at_8"
    return _measured(row, key)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]


def score(
    test_path: Path,
    manifest_path: Path,
    ledger_path: Path,
    output: Path,
    cases_csv: Path | None = None,
) -> dict[str, object]:
    raw = _test_json(test_path)  # Gold is first consumed here, after predictions are frozen.
    manifest = cast(dict[str, object], json.loads(manifest_path.read_text(encoding="utf-8")))
    ledger = cast(dict[str, object], json.loads(ledger_path.read_text(encoding="utf-8")))
    if ledger["manifest_sha256"] != hashlib.sha256(manifest_path.read_bytes()).hexdigest():
        raise ValueError("prediction ledger does not match frozen candidate manifest")
    judgments = {row["question_id"]: row for row in cast(list[dict[str, object]], ledger["rows"])}
    cases: list[dict[str, object]] = []
    for row in cast(list[dict[str, object]], manifest["rows"]):
        paper = build_paper(str(row["paper_id"]), raw[str(row["paper_id"])])
        case = next((item for item in paper.cases if item.id == row["question_id"]), None)
        gold_available = case is not None
        candidates = cast(list[dict[str, object]], row["candidates"])
        units = [
            TrialUnit(
                str(item["unit_id"]),
                int(str(item["start"])),
                int(str(item["end"])),
                str(item["text"]),
            )
            for item in candidates
        ]
        counts = {str(item["unit_id"]): int(str(item["rendered_tokens"])) for item in candidates}
        tei_order = cast(list[int], row["tei_order"])
        prediction = judgments.get(row["question_id"])
        jev_order = (
            cast(list[int], prediction["order"])
            if prediction and prediction["status"] == "assessed"
            else None
        )
        tei_score = score_case(case, [units[i] for i in tei_order], counts) if case else None
        jev_score = (
            score_case(case, [units[i] for i in jev_order], counts)
            if case and jev_order is not None
            else None
        )
        tei_rank = _ranking_metrics(case, [units[i] for i in tei_order]) if case else None
        jev_rank = (
            _ranking_metrics(case, [units[i] for i in jev_order])
            if case and jev_order is not None
            else None
        )
        cases.append(
            {
                "paper_id": row["paper_id"],
                "question_id": row["question_id"],
                "gold_addressable": gold_available,
                "jev_status": prediction["status"] if prediction else "missing",
                "tei_complete": tei_score["complete_evidence"] if tei_score else None,
                "jev_complete": jev_score["complete_evidence"] if jev_score else None,
                "fallback_complete": (jev_score or tei_score)["complete_evidence"]
                if tei_score
                else None,
                "tei_top1": tei_score["top1_intersects_evidence"] if tei_score else None,
                "jev_top1": jev_score["top1_intersects_evidence"] if jev_score else None,
                "candidate_hit": tei_rank["candidate_hit"] if tei_rank else None,
                "tei_ndcg_at_8": tei_rank["ndcg_at_8"] if tei_rank else None,
                "jev_ndcg_at_8": jev_rank["ndcg_at_8"] if jev_rank else None,
                "tei_mrr_at_8": tei_rank["mrr_at_8"] if tei_rank else None,
                "jev_mrr_at_8": jev_rank["mrr_at_8"] if jev_rank else None,
            }
        )
    eligible = [item for item in cases if item["gold_addressable"]]
    paired = [item for item in eligible if item["jev_complete"] is not None]
    differences = [
        float(bool(item["fallback_complete"])) - float(bool(item["tei_complete"]))
        for item in eligible
    ]
    rankable = [item for item in eligible if item["candidate_hit"]]
    ndcg_differences = [
        _fallback_ndcg(item) - _measured(item, "tei_ndcg_at_8") for item in rankable
    ]
    tei_latencies = [
        _measured(row, "tei_elapsed_ms") for row in cast(list[dict[str, object]], manifest["rows"])
    ]
    jev_latencies = [
        _measured(row, "elapsed_ms") for row in cast(list[dict[str, object]], ledger["rows"])
    ]
    report: dict[str, object] = {
        "trial_id": TRIAL_ID,
        "dataset_sha256": TEST_SHA256,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "model": MODEL,
        "rubric_id": NOUL.id,
        "rubric_hash": NOUL.hash,
        "selected_papers": len(cases),
        "human_evidence_addressable": len(eligible),
        "candidate_hit_rate": len(rankable) / len(eligible),
        "jev_assessed": sum(item["jev_status"] == "assessed" for item in cases),
        "jev_missing_or_failed": sum(item["jev_status"] != "assessed" for item in cases),
        "tei_complete": sum(bool(item["tei_complete"]) for item in eligible) / len(eligible),
        "jev_assessed_complete": sum(bool(item["jev_complete"]) for item in paired) / len(paired)
        if paired
        else None,
        "tei_on_jev_assessed_complete": sum(bool(item["tei_complete"]) for item in paired)
        / len(paired)
        if paired
        else None,
        "jev_with_tei_fallback_complete": sum(bool(item["fallback_complete"]) for item in eligible)
        / len(eligible),
        "jev_wins_complete": sum(
            bool(item["jev_complete"]) and not bool(item["tei_complete"]) for item in paired
        ),
        "tei_wins_complete": sum(
            bool(item["tei_complete"]) and not bool(item["jev_complete"]) for item in paired
        ),
        "tei_top1_hit": sum(bool(item["tei_top1"]) for item in eligible) / len(eligible),
        "jev_with_tei_fallback_top1_hit": sum(
            bool(item["jev_top1"] if item["jev_top1"] is not None else item["tei_top1"])
            for item in eligible
        )
        / len(eligible),
        "fallback_minus_tei_paper_cluster_95_interval": _interval(differences),
        "tei_ndcg_at_8": statistics.mean(_measured(item, "tei_ndcg_at_8") for item in rankable)
        if rankable
        else None,
        "jev_with_tei_fallback_ndcg_at_8": statistics.mean(
            _fallback_ndcg(item) for item in rankable
        )
        if rankable
        else None,
        "fallback_minus_tei_ndcg_paper_cluster_95_interval": _interval(ndcg_differences)
        if rankable
        else None,
        "additional_calls_reserved": sum(
            len(cast(list[object], row["candidates"]))
            for row in cast(list[dict[str, object]], manifest["rows"])
            if row["question_id"] in cast(list[str], ledger["reserved"])
        ),
        "input_tokens_reported": sum(
            int(str(item["input_tokens"] or 0)) for item in judgments.values()
        ),
        "output_tokens_reported": sum(
            int(str(item["output_tokens"] or 0)) for item in judgments.values()
        ),
        "tei_rerank_p50_ms": statistics.median(tei_latencies),
        "tei_rerank_p95_ms": _percentile(tei_latencies, 0.95),
        "jev_rerank_p50_ms": statistics.median(jev_latencies),
        "jev_rerank_p95_ms": _percentile(jev_latencies, 0.95),
        "cases": cases,
    }
    _save(output, report)
    if cases_csv is not None:
        cases_csv.parent.mkdir(parents=True, exist_ok=True)
        with cases_csv.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(cases[0]))
            writer.writeheader()
            writer.writerows(cases)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "run", "score"))
    parser.add_argument("--test-json", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cases-csv", type=Path)
    parser.add_argument("--embed-url", default="http://127.0.0.1:18081")
    parser.add_argument("--rerank-url", default="http://127.0.0.1:18083")
    parser.add_argument("--allow-public-live", action="store_true")
    options = parser.parse_args()
    if options.stage == "prepare":
        if options.test_json is None:
            raise ValueError("test JSON is required for preparation")
        result = asyncio.run(
            prepare(options.test_json, options.embed_url, options.rerank_url, options.manifest)
        )
    elif options.stage == "run":
        if options.ledger is None or not options.allow_public_live:
            raise ValueError("ledger and explicit public-live flag are required")
        if os.environ.get("ZENITH_EXTERNAL_PROCESSING_FOR_RERANKING") != "true":
            raise ValueError("reranking processing purpose is not enabled")
        key = os.environ.pop("ZENITH_JEV_API_KEY", None) or getpass.getpass(
            "Public Jev key (hidden): "
        )
        result = asyncio.run(run(options.manifest, options.ledger, key))
    else:
        if options.test_json is None or options.ledger is None or options.output is None:
            raise ValueError("test JSON, ledger, and output are required for scoring")
        result = score(
            options.test_json, options.manifest, options.ledger, options.output, options.cases_csv
        )
    print(
        json.dumps(
            {
                key: len(cast(list[object], value))
                if key == "reserved" and isinstance(value, list)
                else value
                for key, value in result.items()
                if key not in ("rows", "cases")
            },
            default=str,
        )
    )


if __name__ == "__main__":
    main()
