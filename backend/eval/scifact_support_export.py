"""Export compact, source-text-free evidence from two frozen SciFact trials."""

import argparse
import csv
import json
from pathlib import Path
from typing import cast

# Keep this exporter independent of application configuration and credentials.
ARCHIVE_SHA256 = "11c621288d41ac144d29b13b0f8503b3820b7d6e8b1f6ff24dff335c196d76be"
CLAIMS_SHA256 = "86f0435d08fdb65d1aa41d1472684f57e6e71930626497bdf4d7a9ec1a632217"
CORPUS_SHA256 = "b8d6c89624cb2ed74dee8938effc4f5d8bd2086887880af8110d64be4ceade62"
TRIAL_ID = "zenith-scifact-support-oracle-dev-sha-24x2-v1"
LOCKED_TRIAL_ID = "zenith-scifact-support-oracle-dev-sha-next-10x2-v1"
LOCKED_MIN_NOUL = 0.6

FIELDS: tuple[str, ...] = (
    "trial_id",
    "claim_id",
    "document_id",
    "label",
    "rationale_sentence_ids",
    "outcome",
    "noul",
    "strict_accept",
    "failure_code",
    "reported_model",
    "rubric_id",
    "rubric_hash",
    "input_fingerprint",
    "elapsed_ms",
    "input_tokens",
    "output_tokens",
)


def export(dev: Path, locked: Path, csv_path: Path, manifest_path: Path) -> None:
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in (dev, locked)]
    expected = ((TRIAL_ID, 0.8, 48), (LOCKED_TRIAL_ID, LOCKED_MIN_NOUL, 20))
    rows: list[dict[str, object]] = []
    for report, (trial_id, threshold, count) in zip(reports, expected, strict=True):
        if (
            report["trial_id"] != trial_id
            or report["threshold"] != threshold
            or report["archive_sha256"] != ARCHIVE_SHA256
            or report["claims_sha256"] != CLAIMS_SHA256
            or report["corpus_sha256"] != CORPUS_SHA256
            or report["calls_reserved"] != count
            or len(report["rows"]) != count
        ):
            raise ValueError("trial identity or completion does not match the frozen protocol")
        rows.extend(
            {"trial_id": trial_id, **cast(dict[str, object], row)} for row in report["rows"]
        )
    ids = [(row["claim_id"], row["document_id"]) for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("development and locked samples overlap")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **row,
                    "rationale_sentence_ids": ",".join(
                        str(value) for value in cast(list[int], row["rationale_sentence_ids"])
                    ),
                }
            )
    manifest = {
        "source": "AllenAI SciFact public development split",
        "archive_sha256": ARCHIVE_SHA256,
        "claims_sha256": CLAIMS_SHA256,
        "corpus_sha256": CORPUS_SHA256,
        "trials": [
            {
                key: report[key]
                for key in (
                    "trial_id",
                    "model",
                    "rubric_id",
                    "rubric_hash",
                    "threshold",
                    "processing_purpose",
                    "prior_calls",
                    "calls_reserved",
                    "summary",
                )
            }
            for report in reports
        ],
        "case_count": len(rows),
        "total_input_tokens": sum(int(str(row["input_tokens"])) for row in rows),
        "total_output_tokens": sum(int(str(row["output_tokens"])) for row in rows),
        "sum_provider_elapsed_ms": sum(int(str(row["elapsed_ms"])) for row in rows),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", type=Path, required=True)
    parser.add_argument("--locked", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    options = parser.parse_args()
    export(options.dev, options.locked, options.csv, options.manifest)


if __name__ == "__main__":
    main()
