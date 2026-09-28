"""Bounded synthetic multilingual pilot; run only with a frozen local manifest.

The fixture and labels are exploratory and share scenario families across
languages. No historical QASPER prediction or private document is read.
"""

import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from app.core.config import settings
from app.core.hardware import PROFILES
from app.features.retrieval.judging.jev import (
    JevJudge,
    JevQuota,
    ProcessingPolicy,
    PublicScoreRecorder,
    Purpose,
)
from app.features.retrieval.judging.protocol import Candidate, Outcome
from app.features.retrieval.judging.rubrics import CLAIM_SUPPORT_NOUL, NOUL, SCORE6, Formulation
from app.features.retrieval.reranker import TeiReranker

FIXTURE = Path(__file__).parent / "fixtures/evidence-v3-multilingual-synthetic-pilot.json"
MODEL = "jev-1.13.0"
MAX_CALLS = 320
MAX_INPUT_TOKENS = 500_000
MAX_OUTPUT_TOKENS = 30_000
MAX_USD = 3.0
PRICE_USD_PER_INPUT_TOKEN = 42 / 1_000_000_000
STAGE_CAPS = {"score": 48, "support": 64, "ranking": 160, "latency": 48}
TEI_URL = "http://127.0.0.1:18083"

FALSE_CLAIMS = {
    "en": (
        "A calendar notice alone permits working from another country.",
        "Expense claims never require receipts.",
        "A legal hold does not affect backup deletion.",
        "Critical incidents may be reported after 24 hours.",
    ),
    "es": (
        "Basta con un aviso en el calendario para trabajar desde otro país.",
        "Las dietas nunca requieren recibos.",
        "Una retención legal no afecta al borrado de copias.",
        "Los incidentes críticos pueden notificarse después de 24 horas.",
    ),
    "fr": (
        "Une mention au calendrier suffit pour travailler depuis un autre pays.",
        "Les notes de frais ne nécessitent jamais de reçus.",
        "Une obligation de conservation ne change pas l'effacement des sauvegardes.",
        "Les incidents critiques peuvent être signalés après 24 heures.",
    ),
}


def _digest(data: bytes) -> str:
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


def _fixture() -> tuple[str, list[dict[str, Any]]]:
    raw = FIXTURE.read_bytes()
    value: dict[str, Any] = json.loads(raw)
    if value["classification"] != "synthetic" or len(value["groups"]) != 5:
        raise ValueError("unexpected synthetic fixture")
    return _digest(raw), value["groups"]


def _cases(groups: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    ranking: list[dict[str, Any]] = []
    for group in groups:
        for index, query in enumerate(group["queries"]):
            ranking.append(
                {
                    "id": f"{group['language']}:{index}",
                    "language": group["language"],
                    "question": query["text"],
                    "passages": group["passages"],
                    "gold": query["gold"],
                }
            )
    support: list[dict[str, Any]] = []
    for group in groups[:3]:
        language = group["language"]
        for index, passage in enumerate(group["passages"]):
            support.extend(
                (
                    {
                        "id": f"{language}:{index}:yes",
                        "claim": passage.split(".")[0] + ".",
                        "passage": passage,
                        "supported": True,
                    },
                    {
                        "id": f"{language}:{index}:no",
                        "claim": FALSE_CLAIMS[language][index],
                        "passage": passage,
                        "supported": False,
                    },
                )
            )
    return {"score": ranking[:4], "support": support, "ranking": ranking, "latency": ranking[:2]}


def _planned(cases: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return {
        "score": sum(len(case["passages"]) for case in cases["score"]),
        "support": len(cases["support"]),
        "ranking": sum(len(case["passages"]) for case in cases["ranking"]),
        "latency": 2 * sum(len(case["passages"]) for case in cases["latency"]),
    }


async def _tei_orders(cases: list[dict[str, Any]]) -> dict[str, list[int]]:
    tei = TeiReranker(url=TEI_URL, profile=PROFILES["gpu"])
    orders: dict[str, list[int]] = {}
    for case in cases:
        scores = await tei.rank(case["question"], case["passages"])
        order = [item.index for item in scores]
        if sorted(order) != list(range(len(case["passages"]))):
            raise ValueError("TEI did not return a complete ordering")
        orders[case["id"]] = order
    return orders


def _reserved_upper(cases: dict[str, list[dict[str, Any]]]) -> int:
    total = 0
    for stage, group in cases.items():
        rubric = SCORE6 if stage == "score" else CLAIM_SUPPORT_NOUL if stage == "support" else NOUL
        for case in group:
            pairs = (
                [(case["claim"], case["passage"])]
                if stage == "support"
                else [(case["question"], passage) for passage in case["passages"]]
            )
            for question, passage in pairs:
                rendered = json.dumps(
                    {
                        "model": MODEL,
                        "state": {"original_question": question, "candidate_passage": passage},
                        "questions": {"contribution": rubric.question()},
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
                total += (len(rendered) + 4096) * (2 if stage == "latency" else 1)
    return total


async def prepare(directory: Path) -> None:
    fixture_sha, groups = _fixture()
    cases = _cases(groups)
    planned = _planned(cases)
    if any(planned[key] > STAGE_CAPS[key] for key in planned):
        raise ValueError("stage exceeds the approved call cap")
    if sum(planned.values()) > MAX_CALLS:
        raise ValueError("pilot exceeds the approved call cap")
    reserved_upper = _reserved_upper(cases)
    if reserved_upper > MAX_INPUT_TOKENS:
        raise ValueError("pilot exceeds the approved input-token reservation")
    orders = await _tei_orders(cases["ranking"])
    manifest = {
        "run_id": "evidence-v3-hardening-synthetic-pilot-v1",
        "approval": (
            "user reply approving 320 Jev calls, 500000 input tokens, 30000 output tokens, 3 USD"
        ),
        "classification": "synthetic",
        "fixture_sha256": fixture_sha,
        "model": MODEL,
        "tei_url": TEI_URL,
        "tei_orders": orders,
        "planned_calls": planned,
        "conservative_input_reservation": reserved_upper,
        "caps": {
            "calls": MAX_CALLS,
            "input_tokens": MAX_INPUT_TOKENS,
            "output_tokens": MAX_OUTPUT_TOKENS,
            "usd": MAX_USD,
        },
        "capture": "Score raw responses only, ignored .scratch, public/synthetic classification",
        "recovery": "never replay reserved calls automatically after interruption",
    }
    _save(directory / "manifest.json", manifest)
    _save(
        directory / "LIVE_RUN_AUTHORIZATION.json",
        {
            "manifest_sha256": _digest((directory / "manifest.json").read_bytes()),
            "approval": manifest["approval"],
            "credential_source": "ignored backend/.env through Settings; value not exported",
            "purpose_caps": STAGE_CAPS,
            "maximum_wall_seconds": 1200,
            "concurrency": [1, 2],
            "rate": "no automatic retry; at most two concurrent requests",
            "unknown_dispatch": "reserved and never automatically retried",
        },
    )
    print("manifest_sha256=" + _digest((directory / "manifest.json").read_bytes()))
    print("planned_calls=" + str(planned))


async def run(directory: Path, manifest_sha: str) -> None:
    manifest_file = directory / "manifest.json"
    if _digest(manifest_file.read_bytes()) != manifest_sha:
        raise ValueError("frozen manifest hash mismatch")
    manifest: dict[str, Any] = json.loads(manifest_file.read_text(encoding="utf-8"))
    fixture_sha, groups = _fixture()
    if fixture_sha != manifest["fixture_sha256"]:
        raise ValueError("fixture changed after freeze")
    if (directory / "ledger.jsonl").exists():
        raise ValueError("existing ledger: no automatic replay of uncertain dispatch")
    if not settings.jev_api_key:
        raise ValueError("Jev credential unavailable")
    cases = _cases(groups)
    if _planned(cases) != manifest["planned_calls"]:
        raise ValueError("planned calls changed")
    if _reserved_upper(cases) != manifest["conservative_input_reservation"]:
        raise ValueError("input reservation changed")
    key = settings.jev_api_key.get_secret_value()
    quota = JevQuota(MAX_CALLS, MAX_INPUT_TOKENS, max_concurrency=2)
    ledger = directory / "ledger.jsonl"
    _append(ledger, {"event": "start", "manifest_sha256": manifest_sha})
    started = time.monotonic()
    recorder = PublicScoreRecorder(directory / "score_raw", classification="synthetic")

    async def allowed(question: str, candidate: Candidate, purpose: Purpose) -> bool:
        return purpose in {Purpose.RERANKING, Purpose.CLAIM_SUPPORT}

    async def assess(
        stage: str,
        case_id: str,
        question: str,
        passage: str,
        judge: JevJudge,
    ) -> dict[str, Any]:
        identity = uuid5(NAMESPACE_URL, f"{stage}:{case_id}:{passage}")
        candidate = Candidate(identity, passage)
        _append(ledger, {"event": "reserved", "stage": stage, "case": case_id})
        batch = await judge.assess(question, [candidate])
        item = batch.judgments[0]
        result = {
            "event": "result",
            "stage": stage,
            "case": case_id,
            "outcome": item.outcome.value,
            "failure": item.failure_code,
            "rank_value": item.rank_value,
            "grade_distribution": item.grade_distribution,
            "reported_model": item.reported_model,
            "usage_input": batch.usage,
            "usage_output": batch.output_tokens,
            "elapsed_ms": batch.elapsed_ms,
        }
        _append(ledger, result)
        return result

    score = JevJudge(
        api_key=key,
        model=MODEL,
        formulation=Formulation.SCORE6,
        policy=ProcessingPolicy(reranking=True),
        purpose=Purpose.RERANKING,
        authorize=allowed,
        quota=quota,
        max_concurrency=2,
        max_retries=0,
        score_recorder=recorder,
    )
    support = JevJudge(
        api_key=key,
        model=MODEL,
        formulation=Formulation.NOUL,
        rubric=CLAIM_SUPPORT_NOUL,
        policy=ProcessingPolicy(claim_support=True),
        purpose=Purpose.CLAIM_SUPPORT,
        authorize=allowed,
        quota=quota,
        max_concurrency=2,
        max_retries=0,
    )
    noul = JevJudge(
        api_key=key,
        model=MODEL,
        formulation=Formulation.NOUL,
        rubric=NOUL,
        policy=ProcessingPolicy(reranking=True),
        purpose=Purpose.RERANKING,
        authorize=allowed,
        quota=quota,
        max_concurrency=2,
        max_retries=0,
    )
    serial_noul = JevJudge(
        api_key=key,
        model=MODEL,
        formulation=Formulation.NOUL,
        rubric=NOUL,
        policy=ProcessingPolicy(reranking=True),
        purpose=Purpose.RERANKING,
        authorize=allowed,
        quota=quota,
        max_concurrency=1,
        max_retries=0,
    )
    try:
        for stage in ("score", "support", "ranking", "latency"):
            for case in cases[stage]:
                if time.monotonic() - started > 1200:
                    raise TimeoutError("pilot wall-time cap reached")
                if stage == "support":
                    await assess(stage, case["id"], case["claim"], case["passage"], support)
                    continue
                if stage == "latency":
                    for layout, judge in (("serial", serial_noul), ("configured", noul)):
                        candidates = [
                            Candidate(
                                uuid5(NAMESPACE_URL, f"latency:{case['id']}:{index}"), passage
                            )
                            for index, passage in enumerate(case["passages"])
                        ]
                        for index in range(len(candidates)):
                            _append(
                                ledger,
                                {
                                    "event": "reserved",
                                    "stage": stage,
                                    "case": f"{case['id']}:{layout}:{index}",
                                },
                            )
                        batch = await judge.assess(case["question"], candidates)
                        for index, item in enumerate(batch.judgments):
                            _append(
                                ledger,
                                {
                                    "event": "result",
                                    "stage": stage,
                                    "case": f"{case['id']}:{layout}:{index}",
                                    "outcome": item.outcome.value,
                                    "failure": item.failure_code,
                                    "rank_value": item.rank_value,
                                    "reported_model": item.reported_model,
                                    "batch_elapsed_ms": batch.elapsed_ms,
                                    "usage_input": batch.usage,
                                    "usage_output": batch.output_tokens,
                                },
                            )
                    continue
                for index, passage in enumerate(case["passages"]):
                    await assess(
                        stage,
                        f"{case['id']}:{index}",
                        case["question"],
                        passage,
                        score if stage == "score" else noul,
                    )
            print(f"stage={stage} reserved_calls={quota.requests}", flush=True)
    finally:
        await score.aclose()
        await support.aclose()
        await noul.aclose()
        await serial_noul.aclose()
    _append(
        ledger,
        {
            "event": "complete",
            "reserved_calls": quota.requests,
            "reserved_input_tokens": quota.input_tokens,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "published_price_upper_estimate_usd": round(
                quota.input_tokens * PRICE_USD_PER_INPUT_TOKEN, 6
            ),
        },
    )
    print(f"completed reserved_calls={quota.requests} reserved_input_tokens={quota.input_tokens}")


def summarize(directory: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (directory / "ledger.jsonl").read_text().splitlines()]
    results = [row for row in rows if row["event"] == "result"]
    if not rows or rows[-1]["event"] != "complete":
        raise ValueError("incomplete ledger; no qualification summary")
    fixture_sha, groups = _fixture()
    if fixture_sha != manifest["fixture_sha256"]:
        raise ValueError("fixture changed")
    cases = _cases(groups)
    by_stage = {stage: [row for row in results if row["stage"] == stage] for stage in STAGE_CAPS}
    ranking: list[dict[str, Any]] = []
    for case in cases["ranking"]:
        observations = [
            row for row in by_stage["ranking"] if row["case"].startswith(case["id"] + ":")
        ]
        observations.sort(key=lambda row: int(row["case"].rsplit(":", 1)[1]))
        complete = len(observations) == len(case["passages"]) and all(
            row["outcome"] == Outcome.ASSESSED.value for row in observations
        )
        order = (
            sorted(
                range(len(observations)),
                key=lambda index: (-observations[index]["rank_value"], index),
            )
            if complete
            else manifest["tei_orders"][case["id"]]
        )
        ranking.append(
            {
                "id": case["id"],
                "language": case["language"],
                "gold": case["gold"],
                "tei_top1": manifest["tei_orders"][case["id"]][0] == case["gold"],
                "noul_top1": order[0] == case["gold"],
                "noul_complete": complete,
                "actual_provider": "jev" if complete else "tei_fallback",
            }
        )
    support_results = {
        "supported": {"accepted": 0, "total": 0},
        "unsupported": {"accepted": 0, "total": 0},
    }
    labels = {case["id"]: case["supported"] for case in cases["support"]}
    for row in by_stage["support"]:
        label = "supported" if labels[row["case"]] else "unsupported"
        support_results[label]["total"] += 1
        support_results[label]["accepted"] += int(
            row["outcome"] == Outcome.ASSESSED.value and row["rank_value"] >= 0.8
        )
    score_failures: dict[str, int] = {}
    for row in by_stage["score"]:
        if row["failure"]:
            score_failures[row["failure"]] = score_failures.get(row["failure"], 0) + 1
    return {
        "fixture_sha256": fixture_sha,
        "manifest_sha256": _digest((directory / "manifest.json").read_bytes()),
        "classification": "synthetic exploratory development; no locked test",
        "score": {"calls": len(by_stage["score"]), "failure_codes": score_failures},
        "support": support_results,
        "ranking": ranking,
        "latency_calls": len(by_stage["latency"]),
        "accounting": rows[-1],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "summarize"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--manifest-sha256", default="")
    options = parser.parse_args()
    if ".scratch" not in options.directory.resolve().parts:
        raise ValueError("pilot directory must stay under ignored .scratch")
    if options.command == "prepare":
        asyncio.run(prepare(options.directory))
    elif options.command == "run":
        if len(options.manifest_sha256) != 64:
            raise ValueError("run requires the exact frozen manifest hash")
        asyncio.run(run(options.directory, options.manifest_sha256))
    else:
        print(json.dumps(summarize(options.directory), indent=2))


if __name__ == "__main__":
    main()
