"""Local-only ContractNLI evidence benchmark using the official development split.

The public corpus contains real legal documents. This command sends document text
only to the configured local TEI endpoint, never to an external judge.
"""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, cast

from eval.qasper_trial import Case, Paper, evaluate_papers, summarize

ARCHIVE_SHA256 = "e03fc77bbf8b53e2976a250e81d8a294bc3d5e5fb014521e477dee9340d6287b"
DEV_SHA256 = "310af7d661d2ab50ee3700169cef524c75f39fb296bbf5a515c229eb0f42e68e"
DATASET_URL = (
    "https://raw.githubusercontent.com/stanfordnlp/contract-nli/gh-pages/resources/contract-nli.zip"
)
TRIAL_VERSION = "zenith-r1-contract-nli-dev-v1"


def build_document(raw: dict[str, Any], labels: dict[str, Any]) -> tuple[Paper, Counter[str], int]:
    document_id = str(raw["id"])
    text = str(raw["text"])
    spans: list[list[int]] = raw["spans"]
    annotations: dict[str, dict[str, Any]] = raw["annotation_sets"][0]["annotations"]
    counts: Counter[str] = Counter()
    blank_spans = 0
    cases: list[Case] = []
    for label_id, label in sorted(labels.items()):
        annotation = annotations[label_id]
        choice = str(annotation["choice"])
        counts[choice] += 1
        if choice not in {"Entailment", "Contradiction"}:
            continue
        evidence: list[tuple[int, int]] = []
        for raw_index in annotation["spans"]:
            if not isinstance(raw_index, int) or isinstance(raw_index, bool):
                raise ValueError("ContractNLI span index is invalid")
            span_index: int = raw_index
            start, end = spans[span_index]
            if not 0 <= start < end <= len(text):
                raise ValueError("ContractNLI evidence span is invalid")
            if not text[start:end].strip():
                blank_spans += 1
                continue
            evidence.append((start, end))
        if evidence:
            cases.append(
                Case(
                    f"{document_id}:{label_id}",
                    str(label["hypothesis"]),
                    (tuple(evidence),),
                    choice,
                )
            )
    return Paper(document_id, text, tuple(cases), len(labels)), counts, blank_spans


def load_documents(path: Path) -> tuple[tuple[Paper, ...], Counter[str], int]:
    raw_bytes = path.read_bytes()
    if hashlib.sha256(raw_bytes).hexdigest() != DEV_SHA256:
        raise ValueError("ContractNLI development snapshot changed; frozen spans are invalid")
    raw: dict[str, Any] = json.loads(raw_bytes)
    labels: dict[str, Any] = raw["labels"]
    papers: list[Paper] = []
    counts: Counter[str] = Counter()
    blank_spans = 0
    for document in raw["documents"]:
        paper, document_counts, document_blank = build_document(document, labels)
        papers.append(paper)
        counts.update(document_counts)
        blank_spans += document_blank
    return tuple(papers), counts, blank_spans


def run(path: Path, *, tei_url: str) -> dict[str, object]:
    papers, label_counts, blank_spans = load_documents(path)
    evaluation = evaluate_papers(papers, tei_url=tei_url, source_version="contract-nli-v1")
    rows = cast(list[dict[str, object]], evaluation["cases"])
    evaluation["summary_by_choice"] = {
        choice: summarize([row for row in rows if row["label_choice"] == choice])
        for choice in ("Entailment", "Contradiction")
    }
    return {
        "trial_version": TRIAL_VERSION,
        "dataset_url": DATASET_URL,
        "archive_sha256": ARCHIVE_SHA256,
        "dev_sha256": DEV_SHA256,
        "selection": "all 61 ContractNLI official v1 development documents",
        "label_provenance": (
            "ContractNLI human hypothesis labels and character-index evidence spans"
        ),
        "label_counts": dict(label_counts),
        "blank_annotated_spans_skipped": blank_spans,
        "non_evidence_label_policy": "NotMentioned has no evidence target; excluded from recall",
        "external_processing": False,
        **evaluation,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-json", type=Path, required=True)
    parser.add_argument("--tei-url", default="http://127.0.0.1:18081")
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    report = run(options.dev_json, tei_url=options.tei_url)
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {options.output}")


if __name__ == "__main__":
    main()
