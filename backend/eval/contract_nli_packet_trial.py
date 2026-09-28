"""Local-only ContractNLI packet comparison on human evidence spans.

Real contract source text stays in the ignored trial folder and reaches only
local GPU TEI services. NotMentioned hypotheses have no evidence target and
are excluded from the evidence-recall denominator.
"""

import argparse
import asyncio
import hashlib
import json
import statistics
from pathlib import Path
from typing import cast

from eval.contract_nli_common import ARCHIVE_SHA256, DEV_SHA256, load_documents
from eval.qasper_packet_trial import evaluate_papers

TRIAL_VERSION = "zenith-contract-nli-packets-dev-v3"


async def run(dev_json: Path, *, embed_url: str, rerank_url: str) -> dict[str, object]:
    papers, label_counts, blank_spans = load_documents(dev_json)
    report = await evaluate_papers(
        papers,
        dataset_sha256=hashlib.sha256(dev_json.read_bytes()).hexdigest(),
        trial_version=TRIAL_VERSION,
        source_version="contract-nli-v1",
        label_provenance="ContractNLI human hypothesis labels and character-index evidence spans",
        embed_url=embed_url,
        rerank_url=rerank_url,
    )
    rows = cast(list[dict[str, object]], report["rows"])
    by_choice: dict[str, dict[str, object]] = {}
    for choice in ("Entailment", "Contradiction"):
        chosen = [row for row in rows if row["label_choice"] == choice]
        routes: dict[str, object] = {}
        for name in (
            "dense_topk",
            "tei_topk",
            "dense_packet",
            "tei_packet",
            "dense_packet_counter",
            "tei_packet_counter",
        ):
            scores = [cast(dict[str, dict[str, object]], row["routes"])[name] for row in chosen]
            routes[name] = {
                "cases": len(scores),
                "complete_evidence_rate": statistics.mean(
                    float(cast(bool, score["complete_evidence"])) for score in scores
                ),
                "mean_evidence_recall": statistics.mean(
                    cast(float, score["evidence_recall"]) for score in scores
                ),
            }
        by_choice[choice] = routes
    report.update(
        {
            "archive_sha256": ARCHIVE_SHA256,
            "expected_dataset_sha256": DEV_SHA256,
            "label_counts": dict(label_counts),
            "blank_annotated_spans_skipped": blank_spans,
            "non_evidence_label_policy": "NotMentioned excluded from evidence recall",
            "external_processing": False,
            "summary_by_choice": by_choice,
        }
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-json", type=Path, required=True)
    parser.add_argument("--embed-url", default="http://127.0.0.1:18081")
    parser.add_argument("--rerank-url", default="http://127.0.0.1:18083")
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    report = asyncio.run(
        run(options.dev_json, embed_url=options.embed_url, rerank_url=options.rerank_url)
    )
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {options.output}")


if __name__ == "__main__":
    main()
