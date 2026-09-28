"""Neutral ContractNLI source and human-evidence mapping."""

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from eval.qasper_common import Case, Paper

ARCHIVE_SHA256 = "e03fc77bbf8b53e2976a250e81d8a294bc3d5e5fb014521e477dee9340d6287b"
DEV_SHA256 = "310af7d661d2ab50ee3700169cef524c75f39fb296bbf5a515c229eb0f42e68e"


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
