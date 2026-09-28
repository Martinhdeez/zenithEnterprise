"""Export reviewable packet trial artifacts without embedding source text."""

import argparse
import csv
import json
from pathlib import Path
from typing import cast

ROUTES = (
    "dense_topk",
    "tei_topk",
    "dense_packet",
    "tei_packet",
    "dense_packet_counter",
    "tei_packet_counter",
)
METRICS = (
    "complete_evidence",
    "evidence_recall",
    "top1_intersects_evidence",
    "rendered_tokens",
    "selected_units",
    "unresolved",
    "counterevidence_candidates",
    "counterevidence_status",
)


def export(report: dict[str, object], csv_path: Path, manifest_path: Path) -> None:
    """Keep per-case outcomes and provenance; never include dataset passages."""
    rows = cast(list[dict[str, object]], report["rows"])
    fields = ["paper_id", "question_id", "label_choice"] + [
        f"{route}_{metric}" for route in ROUTES for metric in METRICS
    ]
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            routes = cast(dict[str, dict[str, object]], row["routes"])
            exported = {key: row.get(key) for key in fields[:3]}
            for route in ROUTES:
                values = routes[route]
                for metric in METRICS:
                    value = values.get(metric)
                    exported[f"{route}_{metric}"] = (
                        json.dumps(value, separators=(",", ":"))
                        if isinstance(value, list)
                        else value
                    )
            writer.writerow(exported)
    manifest = {key: value for key, value in report.items() if key != "rows"}
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--csv-output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    options = parser.parse_args()
    report = cast(dict[str, object], json.loads(options.input.read_text(encoding="utf-8")))
    export(report, options.csv_output, options.manifest_output)
    print(f"wrote {options.csv_output} and {options.manifest_output}")


if __name__ == "__main__":
    main()
