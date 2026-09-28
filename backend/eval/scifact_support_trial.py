"""Frozen public SciFact claim-support trial for the real Jev Noul adapter.

One oracle evidence abstract per human-labeled claim/document pair. This is a
semantic-assessor study, not end-to-end retrieval or generation. Every outbound
byte comes from the official public SciFact development archive; the script
never reads a tenant database or a private corpus.
"""

import argparse
import asyncio
import hashlib
import json
import os
import statistics
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from app.features.retrieval.judging.jev import JevJudge, JevQuota, ProcessingPolicy, Purpose
from app.features.retrieval.judging.protocol import Candidate, Outcome, ScoreKind
from app.features.retrieval.judging.rubrics import CLAIM_SUPPORT_NOUL, Formulation

ARCHIVE_SHA256 = "11c621288d41ac144d29b13b0f8503b3820b7d6e8b1f6ff24dff335c196d76be"
CLAIMS_SHA256 = "86f0435d08fdb65d1aa41d1472684f57e6e71930626497bdf4d7a9ec1a632217"
CORPUS_SHA256 = "b8d6c89624cb2ed74dee8938effc4f5d8bd2086887880af8110d64be4ceade62"
TRIAL_ID = "zenith-scifact-support-oracle-dev-sha-24x2-v1"
PER_LABEL = 24
PRIOR_CALLS = 930
APPROVED_CALL_CAP = 1000
MIN_NOUL = 0.8
LOCKED_TRIAL_ID = "zenith-scifact-support-oracle-dev-sha-next-10x2-v1"
LOCKED_START = 24
LOCKED_PER_LABEL = 10
LOCKED_PRIOR_CALLS = 978
# Chosen from the first 48 development outcomes before running the disjoint 20.
LOCKED_MIN_NOUL = 0.6


def _checked(path: Path, expected: str) -> list[dict[str, object]]:
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"SciFact file changed: {path.name}")
    return [cast(dict[str, object], json.loads(line)) for line in data.splitlines()]


def select(
    claims_path: Path, corpus_path: Path, *, start: int = 0, per_label: int = PER_LABEL
) -> list[dict[str, object]]:
    """Freeze a balanced sample before reading any Jev outcomes."""
    claims = _checked(claims_path, CLAIMS_SHA256)
    corpus = {int(str(row["doc_id"])): row for row in _checked(corpus_path, CORPUS_SHA256)}
    available: dict[str, list[dict[str, object]]] = {"SUPPORT": [], "CONTRADICT": []}
    for claim in claims:
        evidence = cast(dict[str, list[dict[str, object]]], claim["evidence"])
        for document_id, rationales in evidence.items():
            if not rationales:
                continue
            label = str(rationales[0]["label"])
            if label not in available or any(item["label"] != label for item in rationales):
                continue
            source = corpus.get(int(document_id))
            if source is None:
                raise ValueError("annotated SciFact source is missing")
            abstract = " ".join(cast(list[str], source["abstract"]))
            rendered = f"[1] {source['title']}\n{abstract}"
            if len((str(claim["claim"]) + rendered).encode()) > 10_000:
                continue
            available[label].append(
                {
                    "claim_id": int(str(claim["id"])),
                    "document_id": int(document_id),
                    "label": label,
                    "claim": str(claim["claim"]),
                    "rendered": rendered,
                    "rationale_sentence_ids": cast(list[int], rationales[0]["sentences"]),
                }
            )
    selected: list[dict[str, object]] = []
    for label in ("SUPPORT", "CONTRADICT"):
        ordered = sorted(
            available[label],
            key=lambda row: hashlib.sha256(
                f"{row['claim_id']}:{row['document_id']}".encode()
            ).hexdigest(),
        )
        chosen = ordered[start : start + per_label]
        if len(chosen) != per_label:
            raise ValueError("SciFact sample lacks the declared label balance")
        selected.extend(chosen)
    return selected


def _candidate(row: dict[str, object]) -> Candidate:
    return Candidate(
        uuid5(NAMESPACE_URL, f"scifact:{row['claim_id']}:{row['document_id']}"),
        str(row["rendered"]),
    )


def _ledger(path: Path, trial_id: str, prior_calls: int) -> dict[str, object]:
    if path.exists():
        state = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        if state.get("trial_id") != trial_id or state.get("prior_calls") != prior_calls:
            raise ValueError("live-call ledger belongs to another trial")
        return state
    return {"trial_id": trial_id, "prior_calls": prior_calls, "reserved": [], "rows": []}


def _save(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _score(rows: list[dict[str, object]], per_label: int) -> dict[str, object]:
    summary: dict[str, object] = {}
    for label in ("SUPPORT", "CONTRADICT"):
        chosen = [row for row in rows if row["label"] == label]
        assessed = [row for row in chosen if row["outcome"] == "assessed"]
        accepted = sum(bool(row["strict_accept"]) for row in assessed)
        summary[label] = {
            "planned": per_label,
            "completed": len(chosen),
            "assessed": len(assessed),
            "accepted": accepted,
            "accept_rate_among_assessed": accepted / len(assessed) if assessed else None,
            "mean_noul_among_assessed": (
                statistics.mean(float(str(row["noul"])) for row in assessed) if assessed else None
            ),
        }
    return summary


async def run(
    cases: list[dict[str, object]],
    *,
    api_key: str,
    ledger_path: Path,
    output_path: Path,
    trial_id: str = TRIAL_ID,
    prior_calls: int = PRIOR_CALLS,
    threshold: float = MIN_NOUL,
    per_label: int = PER_LABEL,
) -> dict[str, object]:
    state = _ledger(ledger_path, trial_id, prior_calls)
    reserved = cast(list[str], state["reserved"])
    if prior_calls + len(cases) > APPROVED_CALL_CAP:
        raise ValueError("Jev approved call cap would be exceeded")
    if len(set(reserved)) != len(reserved):
        raise ValueError("duplicate live-call reservation")
    frozen = {str(_candidate(row).id): row for row in cases}

    async def permitted(question: str, candidate: Candidate, purpose: Purpose) -> bool:
        row = frozen.get(str(candidate.id))
        return bool(
            purpose is Purpose.CLAIM_SUPPORT
            and row is not None
            and question == row["claim"]
            and candidate.text == row["rendered"]
        )

    judge = JevJudge(
        api_key=api_key,
        formulation=Formulation.NOUL,
        rubric=CLAIM_SUPPORT_NOUL,
        policy=ProcessingPolicy(claim_support=True),
        purpose=Purpose.CLAIM_SUPPORT,
        authorize=permitted,
        quota=JevQuota(len(cases), 1_000_000, max_concurrency=1),
        max_concurrency=1,
        deadline_seconds=20.0,
    )
    try:
        for index, row in enumerate(cases, 1):
            candidate = _candidate(row)
            identity = str(candidate.id)
            if identity in reserved:
                continue  # A reserved-but-missing result is unknown, never replayed for free.
            if prior_calls + len(reserved) >= APPROVED_CALL_CAP:
                raise ValueError("Jev approved call cap exhausted")
            reserved.append(identity)
            _save(ledger_path, state)  # Reserve before dispatch, including failed calls.
            batch = await judge.assess(str(row["claim"]), [candidate])
            judgment = batch.judgments[0]
            value = judgment.rank_value
            valid = (
                judgment.outcome is Outcome.ASSESSED
                and judgment.score_kind is ScoreKind.MODEL_PROBABILITY
                and value is not None
                and judgment.rubric_id == CLAIM_SUPPORT_NOUL.id
                and judgment.rubric_hash == CLAIM_SUPPORT_NOUL.hash
            )
            result = {
                "claim_id": row["claim_id"],
                "document_id": row["document_id"],
                "label": row["label"],
                "rationale_sentence_ids": row["rationale_sentence_ids"],
                "outcome": "assessed" if valid else "failed",
                "noul": value if valid else None,
                "strict_accept": bool(valid and value is not None and value >= threshold),
                "failure_code": judgment.failure_code,
                "reported_model": judgment.reported_model,
                "rubric_id": judgment.rubric_id,
                "rubric_hash": judgment.rubric_hash,
                "input_fingerprint": judgment.input_fingerprint,
                "elapsed_ms": batch.elapsed_ms,
                "input_tokens": batch.usage,
                "output_tokens": batch.output_tokens,
            }
            cast(list[dict[str, object]], state["rows"]).append(result)
            _save(ledger_path, state)
            print(f"assessed public SciFact pair {index}/{len(cases)}", flush=True)
    finally:
        await judge.aclose()
    rows = cast(list[dict[str, object]], state["rows"])
    report: dict[str, object] = {
        "trial_id": trial_id,
        "source": "AllenAI SciFact official development data, public expert rationale labels",
        "archive_sha256": ARCHIVE_SHA256,
        "claims_sha256": CLAIMS_SHA256,
        "corpus_sha256": CORPUS_SHA256,
        "model": "jev-1.13.0",
        "rubric_id": CLAIM_SUPPORT_NOUL.id,
        "rubric_hash": CLAIM_SUPPORT_NOUL.hash,
        "threshold": threshold,
        "selection": f"{per_label} SHA-selected claim/document pairs per class",
        "processing_purpose": Purpose.CLAIM_SUPPORT.value,
        "prior_calls": prior_calls,
        "calls_reserved": len(reserved),
        "summary": _score(rows, per_label),
        "rows": rows,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--allow-public-live", action="store_true")
    parser.add_argument("--locked", action="store_true")
    options = parser.parse_args()
    trial_id = LOCKED_TRIAL_ID if options.locked else TRIAL_ID
    prior_calls = LOCKED_PRIOR_CALLS if options.locked else PRIOR_CALLS
    per_label = LOCKED_PER_LABEL if options.locked else PER_LABEL
    threshold = LOCKED_MIN_NOUL if options.locked else MIN_NOUL
    start = LOCKED_START if options.locked else 0
    cases = select(options.claims, options.corpus, start=start, per_label=per_label)
    if options.preflight:
        print(
            json.dumps(
                {
                    "trial_id": trial_id,
                    "cases": len(cases),
                    "labels": {label: per_label for label in ("SUPPORT", "CONTRADICT")},
                    "max_rendered_bytes": max(
                        len((str(row["claim"]) + str(row["rendered"])).encode()) for row in cases
                    ),
                    "prior_calls": prior_calls,
                    "planned_total_calls": prior_calls + len(cases),
                    "threshold": threshold,
                }
            )
        )
        return
    if not options.allow_public_live:
        raise ValueError("live public Jev calls require --allow-public-live")
    if os.environ.get("ZENITH_EXTERNAL_PROCESSING_FOR_CLAIM_SUPPORT") != "true":
        raise ValueError("claim-support egress purpose is not enabled")
    key = os.environ.pop("ZENITH_JEV_API_KEY", None)
    if not key:
        raise ValueError("Jev credential is not securely available")
    report = asyncio.run(
        run(
            cases,
            api_key=key,
            ledger_path=options.ledger,
            output_path=options.output,
            trial_id=trial_id,
            prior_calls=prior_calls,
            threshold=threshold,
            per_label=per_label,
        )
    )
    print(f"wrote {options.output}; reserved {report['calls_reserved']} public calls")


if __name__ == "__main__":
    main()
