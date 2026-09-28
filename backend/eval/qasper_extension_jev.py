"""Authorized remaining-176-paper extension, reusing the frozen Noul protocol.

Never reissues the historical 240 papers. Preparation removes answer annotations;
only the score stage consumes labels. The combined result is descriptive reuse,
not a new independent 416-paper experiment. Keep all artifacts in ignored storage.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from app.core.config import settings
from eval import qasper_heldout_jev as original

TRIAL_ID = "zenith-qasper-test-remaining176-fixed8-noul-v1"
CALL_CAP = 1408
USD_CAP = 1.0


def plan_for(baseline: Path, manifest_sha256: str = "") -> original.TrialPlan:
    if hashlib.sha256(baseline.read_bytes()).hexdigest() != original.MANIFEST_SHA256:
        raise ValueError("historical manifest identity changed")
    rows = json.loads(baseline.read_text(encoding="utf-8"))["rows"]
    excluded = frozenset(str(row["paper_id"]) for row in rows)
    if len(excluded) != 240:
        raise ValueError("historical cohort is not 240 distinct papers")
    return original.TrialPlan(
        trial_id=TRIAL_ID,
        papers=176,
        manifest_sha256=manifest_sha256,
        additional_call_cap=CALL_CAP,
        additional_usd_cap=USD_CAP,
        input_token_cap=20_000_000,  # $0.84 conservative reservation at published rate.
        excluded_papers=excluded,
    )


def combine(baseline: Path, output: Path) -> None:
    """Join saved candidates and predictions without new dispatch or relabeling."""
    manifests = [
        json.loads((baseline / "heldout-240-frozen-manifest.json").read_text(encoding="utf-8")),
        json.loads((output / "manifest.json").read_text(encoding="utf-8")),
    ]
    ledgers = [
        json.loads((baseline / "heldout-240-jev-ledger.json").read_text(encoding="utf-8")),
        json.loads((output / "ledger.json").read_text(encoding="utf-8")),
    ]
    for ledger, path in zip(
        ledgers,
        [baseline / "heldout-240-frozen-manifest.json", output / "manifest.json"],
        strict=True,
    ):
        if ledger["manifest_sha256"] != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError("prediction identity mismatch")
    rows = [row for manifest in manifests for row in manifest["rows"]]
    if len(rows) != 416 or len({row["paper_id"] for row in rows}) != 416:
        raise ValueError("combined cohort is incomplete or overlapping")
    shared_fields = (
        "dataset_sha256",
        "archive_sha256",
        "candidate_policy",
        "tei_model",
        "jev_model",
        "jev_rubric_id",
        "jev_rubric_hash",
    )
    if any(manifests[0][key] != manifests[1][key] for key in shared_fields):
        raise ValueError("combined trials used different datasets, candidates, or judges")
    component_trials: list[dict[str, Any]] = []
    for manifest, ledger, path in zip(
        manifests,
        ledgers,
        [baseline / "heldout-240-frozen-manifest.json", output / "manifest.json"],
        strict=True,
    ):
        if (
            len(ledger["reserved"]) != len(manifest["rows"])
            or sum(len(row["candidates"]) for row in manifest["rows"]) != manifest["planned_calls"]
        ):
            raise ValueError("component paper reservations or planned calls are inconsistent")
        component_trials.append(
            {
                "trial_id": manifest["trial_id"],
                "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "planned_calls": manifest["planned_calls"],
                "conservative_input_tokens": manifest["conservative_input_tokens"],
                "approved_additional_calls": manifest["approved_additional_calls"],
                "approved_additional_usd": manifest["approved_additional_usd"],
            }
        )
    combined: dict[str, Any] = {
        "trial_id": "zenith-qasper-test-416-combined-descriptive-v1",
        **{key: manifests[0][key] for key in shared_fields},
        "selection": "historical 240 plus separately frozen remaining 176; descriptive reuse",
        "component_trials": component_trials,
        "planned_calls": sum(int(item["planned_calls"]) for item in component_trials),
        "conservative_input_tokens": sum(
            int(item["conservative_input_tokens"]) for item in component_trials
        ),
        "rows": rows,
    }
    manifest_path = output / "combined-manifest.json"
    original._save(manifest_path, combined)  # pyright: ignore[reportPrivateUsage]
    original._save(  # pyright: ignore[reportPrivateUsage]
        output / "combined-ledger.json",
        {
            "trial_id": combined["trial_id"],
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "reserved": [qid for ledger in ledgers for qid in ledger["reserved"]],
            "rows": [row for ledger in ledgers for row in ledger["rows"]],
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "run", "score"))
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--manifest-sha256", default="")
    parser.add_argument("--allow-public-live", action="store_true")
    args = parser.parse_args()
    baseline, output = args.baseline_dir, args.output_dir
    manifest, ledger = output / "manifest.json", output / "ledger.json"
    plan = plan_for(baseline / "heldout-240-frozen-manifest.json", args.manifest_sha256)
    test_json = baseline / "qasper-test-v0.3.json"
    if args.stage == "prepare":
        if manifest.exists() or ledger.exists():
            raise ValueError("refusing to overwrite a frozen study")
        asyncio.run(
            original.prepare(
                test_json, "http://127.0.0.1:18081", "http://127.0.0.1:18083", manifest, plan=plan
            )
        )
        print("Freeze before dispatch:", hashlib.sha256(manifest.read_bytes()).hexdigest())
    elif args.stage == "run":
        if not args.allow_public_live or len(args.manifest_sha256) != 64:
            raise ValueError("explicit public-live flag and preregistered hash required")
        if not settings.external_processing_for_reranking:
            raise ValueError("public reranking processing purpose is not enabled")
        if not settings.jev_api_key or not settings.jev_api_key.get_secret_value():
            raise ValueError("configure ZENITH_JEV_API_KEY in the ignored local environment")
        result = asyncio.run(
            original.run(manifest, ledger, settings.jev_api_key.get_secret_value(), plan=plan)
        )
        print("Reserved papers:", len(result["reserved"]))  # type: ignore[arg-type]
    else:
        if (
            not args.manifest_sha256
            or hashlib.sha256(manifest.read_bytes()).hexdigest() != plan.manifest_sha256
        ):
            raise ValueError("scoring requires the preregistered manifest hash")
        original.score(test_json, manifest, ledger, output / "new176.json", output / "new176.csv")
        combine(baseline, output)
        original.score(
            test_json,
            output / "combined-manifest.json",
            output / "combined-ledger.json",
            output / "combined416.json",
            output / "combined416.csv",
        )
        for name in ("new176", "combined416"):
            report = json.loads((output / f"{name}.json").read_text(encoding="utf-8"))
            print(json.dumps({k: v for k, v in report.items() if k != "cases"}))


if __name__ == "__main__":
    main()
