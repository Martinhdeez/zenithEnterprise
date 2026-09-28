"""Offline summary of frozen synthetic Jev option and atomic-question ledgers."""

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, cast


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def summarize(directory: Path, first_pilot: Path) -> dict[str, object]:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    results = [row for row in _rows(directory / "ledger.jsonl") if row.get("event") == "result"]
    old = [
        row
        for row in _rows(first_pilot / "ledger.jsonl")
        if row.get("event") == "result" and row.get("stage") == "ranking"
    ]
    old_baseline: dict[str, dict[int, float]] = defaultdict(dict)
    for row in old:
        language, query, index = row["case"].split(":")
        if row.get("outcome") == "assessed":
            old_baseline[f"v1:{language}:{query}"][int(index)] = row["rank_value"]
    by_case: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in results:
        by_case[row["case"]][row["variant"]] = row
    gold: dict[str, int] = manifest["gold"]
    variants: dict[str, dict[str, object]] = {}
    case_table: list[dict[str, Any]] = []
    for case_id, expected in gold.items():
        observed: dict[str, int | None] = {"tei": manifest["tei_orders"][case_id][0]}
        baseline = old_baseline.get(case_id, {})
        if case_id.startswith("v2:"):
            baseline = {
                index: row["value"]
                for index in range(4)
                if (row := by_case[case_id].get(f"baseline:{index}"))
                and row.get("outcome") == "assessed"
            }
        observed["baseline"] = (
            max(baseline, key=lambda index: (baseline[index], -index))
            if len(baseline) == 4
            else None
        )
        for variant in ("packed", "choice"):
            row = by_case[case_id].get(variant)
            values = row.get("values") if row else None
            if isinstance(values, list):
                probabilities = cast(list[float], values)
                observed[variant] = (
                    max(range(4), key=lambda index: (probabilities[index], -index))
                    if len(probabilities) == 4
                    else None
                )
            else:
                observed[variant] = None
        case_table.append(
            {
                "case": case_id,
                "language": case_id.split(":")[1],
                "gold": expected,
                "top1": observed,
                "correct": {
                    key: value == expected if value is not None else None
                    for key, value in observed.items()
                },
            }
        )
    for variant in ("tei", "baseline", "packed", "choice"):
        valid = [row for row in case_table if row["top1"][variant] is not None]
        correct = sum(row["correct"][variant] is True for row in valid)
        elapsed = [
            row["elapsed_ms"]
            for row in results
            if row["variant"] == variant and row.get("outcome") == "assessed"
        ]
        variants[variant] = {
            "valid_cases": len(valid),
            "total_cases": len(case_table),
            "top1_correct": correct,
            "top1_rate_among_valid": round(correct / len(valid), 4) if valid else None,
            "per_call_median_ms": statistics.median(elapsed) if elapsed else None,
        }
    failures = [
        {"case": row["case"], "variant": row["variant"], "failure": row["failure"]}
        for row in results
        if row.get("outcome") == "failed"
    ]
    repeats: list[dict[str, object]] = []
    for case_id in ("v2:it:0", "v2:ja:0"):
        for variant in ("packed", "choice"):
            first = by_case[case_id].get(variant)
            second = by_case[case_id].get(f"{variant}_repeat")
            repeats.append(
                {
                    "case": case_id,
                    "variant": variant,
                    "first": first.get("values") if first else None,
                    "repeat": second.get("values") if second else None,
                    "same_values": bool(
                        first and second and first.get("values") == second.get("values")
                    ),
                }
            )
    ending = _rows(directory / "ledger.jsonl")[-1]
    return {
        "classification": "exploratory synthetic; translated cases are dependent",
        "manifest_sha256": _sha((directory / "manifest.json").read_bytes()),
        "variants": variants,
        "failures": failures,
        "repeats": repeats,
        "usage": ending,
        "cases": case_table,
    }


def _sha(raw: bytes) -> str:
    import hashlib

    return hashlib.sha256(raw).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--first-pilot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if ".scratch" not in output.parts:
        raise ValueError("per-case exploratory output must stay ignored")
    summary = summarize(args.directory, args.first_pilot)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key != "cases"}, indent=2))


if __name__ == "__main__":
    main()
