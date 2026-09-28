"""Neutral QASPER source rendering and accepted-evidence scoring."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.features.ingestion.chunking.chunker import chunk_stream
from eval.evidence_common import TrialUnit, choose, covers_all, displayed_range

DEV_SHA256 = "2ae7ee62a65b1c4225791c70de80c2aad4e8998cf1fd4f09a53103db4f21af93"
DOCUMENT_COUNT = 64


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    question: str
    # Multiple human annotations are alternative valid evidence sets.
    evidence_options: tuple[tuple[tuple[int, int], ...], ...]
    label_choice: str | None = None


@dataclass(frozen=True, slots=True)
class Paper:
    id: str
    text: str
    cases: tuple[Case, ...]
    questions: int


def build_paper(paper_id: str, raw: dict[str, Any]) -> Paper:
    """Render original paragraphs once, retaining exact source coordinates."""
    parts: list[str] = []
    paragraph_ranges: dict[str, list[tuple[int, int]]] = {}
    cursor = 0
    for section in raw["full_text"]:
        title = str(section["section_name"] or "").strip()
        if title:
            heading = f"# {title}\n"
            parts.append(heading)
            cursor += len(heading)
        for paragraph in section["paragraphs"]:
            if not paragraph:
                continue
            original = str(paragraph)
            start = cursor
            parts.append(original)
            cursor += len(original)
            paragraph_ranges.setdefault(original, []).append((start, cursor))
            parts.append("\n\n")
            cursor += 2
    text = "".join(parts)
    cases: list[Case] = []
    for qa in raw["qas"]:
        alternatives: list[tuple[tuple[int, int], ...]] = []
        for annotation in qa["answers"]:
            answer = annotation["answer"]
            evidence: list[str] = answer["evidence"]
            if answer["unanswerable"] or not evidence:
                continue
            # Repeated paragraph strings have ambiguous positions in this export.
            # Exclude only that annotation; another annotator may be mappable.
            if any(len(paragraph_ranges.get(passage, ())) != 1 for passage in evidence):
                continue
            ranges = tuple(paragraph_ranges[passage][0] for passage in evidence)
            if ranges and ranges not in alternatives:
                alternatives.append(ranges)
        if alternatives:
            cases.append(Case(str(qa["question_id"]), str(qa["question"]), tuple(alternatives)))
    return Paper(paper_id, text, tuple(cases), len(raw["qas"]))


def load_papers(path: Path) -> tuple[Paper, ...]:
    raw_bytes = path.read_bytes()
    if hashlib.sha256(raw_bytes).hexdigest() != DEV_SHA256:
        raise ValueError("QASPER development snapshot changed; frozen labels are invalid")
    raw: dict[str, dict[str, Any]] = json.loads(raw_bytes)
    ids = sorted(raw, key=lambda paper_id: hashlib.sha256(paper_id.encode()).hexdigest())[
        :DOCUMENT_COUNT
    ]
    return tuple(build_paper(paper_id, raw[paper_id]) for paper_id in ids)


def score_case(
    case: Case, ranked: list[TrialUnit], token_counts: dict[str, int]
) -> dict[str, float | bool | int]:
    selected = choose(ranked, token_counts=token_counts)
    # An answer is supported if all evidence in at least one independent human
    # annotation is visible. Partial recall is the best matching annotation.
    complete = any(covers_all(selected, option) for option in case.evidence_options)
    recall = max(
        sum(covers_all(selected, (gold,)) for gold in option) / len(option)
        for option in case.evidence_options
    )
    top1 = any(
        any(ranked[0].start < end and ranked[0].end > start for start, end in option)
        for option in case.evidence_options
    )
    return {
        "complete_evidence": complete,
        "evidence_recall": recall,
        "top1_intersects_evidence": top1,
        "rendered_tokens": sum(token_counts[unit.id] for unit in selected),
        "selected_units": len(selected),
    }


def legacy_units_for_paper(paper: Paper) -> list[TrialUnit]:
    legacy: list[TrialUnit] = []
    for index, item in enumerate(chunk_stream(paper.text)):
        start, end = displayed_range(paper.text, item.char_start, item.char_end)
        if paper.text[start:end] != item.text:
            raise ValueError("legacy display text does not match source coordinates")
        legacy.append(TrialUnit(f"legacy:{index}", start, end, item.text))
    if not legacy:
        raise ValueError("selected paper has no retrievable units")
    return legacy
