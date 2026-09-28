"""Reprobe four seen QASPER Score failures with new pre-parser capture.

Historical raw responses were lost. This reconstructs the input from the
pinned public development snapshot and records *new* responses separately.
"""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from app.core.config import settings
from app.features.retrieval.judging.jev import (
    JevJudge,
    JevQuota,
    ProcessingPolicy,
    PublicScoreRecorder,
    Purpose,
)
from app.features.retrieval.judging.protocol import Candidate
from app.features.retrieval.judging.rubrics import SCORE6, Formulation
from eval.qasper_common import build_paper, legacy_units_for_paper

REPORT = Path(__file__).parent / "reports/evidence-v3-qasper-judge-live-2026-09-26.json"
DEV_SHA256 = "2ae7ee62a65b1c4225791c70de80c2aad4e8998cf1fd4f09a53103db4f21af93"
MODEL = "jev-1.13.0"
MAX_CALLS = 32
GLOBAL_INPUT_CAP = 500_000
GLOBAL_SCORE_CAP = 48
PRIOR_INPUT = 42_162
PRIOR_SCORE_CALLS = 16


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")


def _append(path: Path, value: object) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(value, ensure_ascii=False) + "\n")
        output.flush()
        os.fsync(output.fileno())


def _requests(dev_json: Path) -> tuple[list[tuple[str, str, Candidate]], int]:
    raw = dev_json.read_bytes()
    if _sha(raw) != DEV_SHA256:
        raise ValueError("QASPER development source hash changed")
    dataset: dict[str, dict[str, Any]] = json.loads(raw)
    report: dict[str, Any] = json.loads(REPORT.read_text(encoding="utf-8"))
    failed = [
        row for row in report["rows"] if row["routes"]["jev_score6"]["status"] == "unavailable"
    ][:4]
    if len(failed) != 4:
        raise ValueError("historical Score failure selection changed")
    requests: list[tuple[str, str, Candidate]] = []
    upper = 0
    for row in failed:
        paper = build_paper(row["paper_id"], dataset[row["paper_id"]])
        case = next(case for case in paper.cases if case.id == row["question_id"])
        by_id = {
            str(uuid5(NAMESPACE_URL, f"qasper-v0.3:{paper.id}:{unit.id}")): unit
            for unit in legacy_units_for_paper(paper)
        }
        for index, candidate_id in enumerate(row["candidate_ids"]):
            unit = by_id.get(candidate_id)
            if unit is None:
                raise ValueError("historical candidate cannot be reconstructed")
            candidate = Candidate(UUID(candidate_id), unit.text)
            request = {
                "model": MODEL,
                "state": {"original_question": case.question, "candidate_passage": unit.text},
                "questions": {"contribution": SCORE6.question()},
            }
            rendered = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
            upper += len(rendered) + 4096
            requests.append((f"{paper.id}:{case.id}:{index}", case.question, candidate))
    if len(requests) != MAX_CALLS:
        raise ValueError("reconstructed Score call count changed")
    return requests, upper


def prepare(dev_json: Path, directory: Path) -> None:
    requests, upper = _requests(dev_json)
    if PRIOR_SCORE_CALLS + len(requests) > GLOBAL_SCORE_CAP:
        raise ValueError("Score purpose cap exceeded")
    if PRIOR_INPUT + upper > GLOBAL_INPUT_CAP:
        raise ValueError("approved input reservation exceeded")
    manifest = {
        "run_id": "evidence-v3-score-seen-reprobe-v1",
        "classification": "public QASPER development, previously inspected",
        "historical_status": (
            "eight old batch failures have no raw body; first four batches selected"
        ),
        "dataset_sha256": DEV_SHA256,
        "historical_report_sha256": _sha(REPORT.read_bytes()),
        "model": MODEL,
        "new_calls": len(requests),
        "conservative_input_reservation": upper,
        "prior_pilot_input": PRIOR_INPUT,
        "case_ids": [case_id for case_id, _, _ in requests],
        "input_hashes": [
            _sha((question + "\0" + candidate.text).encode()) for _, question, candidate in requests
        ],
        "authorization": "remaining 32 Score calls within approved 48; cumulative input <= 500000",
        "capture": "new raw public Score responses in ignored .scratch only",
        "recovery": "reserved calls are never automatically retried",
    }
    _save(directory / "manifest.json", manifest)
    print("manifest_sha256=" + _sha((directory / "manifest.json").read_bytes()))
    print(f"planned_calls={len(requests)} conservative_input_reservation={upper}")


async def run(dev_json: Path, directory: Path, manifest_sha: str) -> None:
    manifest_file = directory / "manifest.json"
    if _sha(manifest_file.read_bytes()) != manifest_sha:
        raise ValueError("frozen manifest hash mismatch")
    manifest: dict[str, Any] = json.loads(manifest_file.read_text(encoding="utf-8"))
    if _sha(REPORT.read_bytes()) != manifest["historical_report_sha256"]:
        raise ValueError("historical report changed")
    requests, upper = _requests(dev_json)
    if (
        upper != manifest["conservative_input_reservation"]
        or [case_id for case_id, _, _ in requests] != manifest["case_ids"]
    ):
        raise ValueError("reconstructed inputs changed")
    if (directory / "ledger.jsonl").exists():
        raise ValueError("existing dispatch ledger; uncertain calls cannot be replayed")
    if not settings.jev_api_key:
        raise ValueError("Jev credential unavailable")
    quota = JevQuota(MAX_CALLS, GLOBAL_INPUT_CAP - PRIOR_INPUT, max_concurrency=1)
    recorder = PublicScoreRecorder(directory / "score_raw", classification="public")

    async def allowed(question: str, candidate: Candidate, purpose: Purpose) -> bool:
        return purpose is Purpose.RERANKING and candidate.id in {item.id for _, _, item in requests}

    judge = JevJudge(
        api_key=settings.jev_api_key.get_secret_value(),
        model=MODEL,
        formulation=Formulation.SCORE6,
        policy=ProcessingPolicy(reranking=True),
        purpose=Purpose.RERANKING,
        authorize=allowed,
        quota=quota,
        max_concurrency=1,
        max_retries=0,
        score_recorder=recorder,
    )
    ledger = directory / "ledger.jsonl"
    _append(ledger, {"event": "start", "manifest_sha256": manifest_sha})
    try:
        for case_id, question, candidate in requests:
            _append(ledger, {"event": "reserved", "case": case_id})
            batch = await judge.assess(question, [candidate])
            item = batch.judgments[0]
            _append(
                ledger,
                {
                    "event": "result",
                    "case": case_id,
                    "outcome": item.outcome.value,
                    "failure": item.failure_code,
                    "reported_model": item.reported_model,
                    "usage_input": batch.usage,
                    "usage_output": batch.output_tokens,
                    "elapsed_ms": batch.elapsed_ms,
                },
            )
    finally:
        await judge.aclose()
    _append(
        ledger,
        {
            "event": "complete",
            "reserved_calls": quota.requests,
            "accounted_input_tokens": quota.input_tokens,
        },
    )
    print(f"completed reserved_calls={quota.requests} accounted_input_tokens={quota.input_tokens}")


def summarize(directory: Path) -> None:
    rows = [json.loads(line) for line in (directory / "ledger.jsonl").read_text().splitlines()]
    if rows[-1]["event"] != "complete":
        raise ValueError("incomplete dispatch ledger")
    results = [row for row in rows if row["event"] == "result"]
    failures: dict[str, int] = {}
    for row in results:
        if row["failure"]:
            failures[row["failure"]] = failures.get(row["failure"], 0) + 1
    raw = list((directory / "score_raw").glob("*.json"))
    masses: list[float] = []
    for path in raw:
        payload = json.loads(path.read_text(encoding="utf-8"))
        answer = json.loads(payload["raw_response_utf8"])["answers"]["contribution"]
        masses.append(sum(answer["probabilities"].values()))
    print(
        json.dumps(
            {
                "new_requests": len(results),
                "new_raw_bodies": len(raw),
                "failure_codes": failures,
                "mass_min": min(masses),
                "mass_max": max(masses),
                "accounted_input_tokens": rows[-1]["accounted_input_tokens"],
                "historical_raw_recovered": False,
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "summarize"))
    parser.add_argument("--dev-json", type=Path)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--manifest-sha256", default="")
    options = parser.parse_args()
    if ".scratch" not in options.directory.resolve().parts:
        raise ValueError("diagnostics must stay under ignored .scratch")
    if options.command == "summarize":
        summarize(options.directory)
    elif options.dev_json is None:
        raise ValueError("public development JSON is required")
    elif options.command == "prepare":
        prepare(options.dev_json, options.directory)
    else:
        if len(options.manifest_sha256) != 64:
            raise ValueError("run requires the frozen manifest SHA-256")
        asyncio.run(run(options.dev_json, options.directory, options.manifest_sha256))


if __name__ == "__main__":
    main()
