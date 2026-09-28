"""R1 isolated grouping versus resegmented/re-embedded public-source trial.

The snapshot and source-span labels are fixed before scoring. This is exploratory:
one agent-labeled EPA page cannot qualify a new production segmentation default.
"""

import argparse
import hashlib
import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID, uuid5

import httpx

from app.features.ingestion.chunking.chunker import chunk_stream
from app.features.ingestion.chunking.lossless import Source, atomic_spans, structural_groups
from eval.embedder import TeiEmbedder

SNAPSHOT_SHA256 = "94e65678ac85eb7aacbb351113021c29bda3a8b257e6a81b31b0e40f79176b5f"
SOURCE_URL = "https://www.epa.gov/ground-water-and-drinking-water/basic-information-about-lead-drinking-water"
EMBED_MODEL = "BAAI/bge-m3@5617a9f61b028005a4858fdac845db406aefb181"
TRIAL_VERSION = "zenith-r1-epa-lead-v1"
SOURCE_ID = uuid5(UUID("5544de04-df02-4f26-bbb3-c950ef8db1e1"), SOURCE_URL)
RENDER_BUDGET_TOKENS = 1100
TOP_K = 8

# Agent-reviewed spans in the frozen public snapshot, 1-based extracted-text lines.
# The labels were not independently adjudicated, and this is not a clinical test.
QUERIES = (
    (
        "boiling-and-formula",
        "Does boiling remove lead and which tap water should be used for baby formula?",
        (93,),
    ),
    (
        "shower-qualification",
        "Can children shower in water containing lead, and when might advice differ?",
        (68, 69),
    ),
    (
        "filter-care",
        "What must a filter do to reduce lead and what water must not pass through it?",
        (91,),
    ),
    (
        "children-exposure",
        "What blood lead level triggers public health action, and why can exposure "
        "have several sources?",
        (43, 44),
    ),
    (
        "lead-free",
        "What does lead-free mean for pipe surfaces compared with solder and flux?",
        (23,),
    ),
)


@dataclass(frozen=True, slots=True)
class TrialUnit:
    id: str
    start: int
    end: int
    text: str


def line_ranges(text: str) -> tuple[tuple[int, int], ...]:
    cursor = 0
    result: list[tuple[int, int]] = []
    for line in text.splitlines(keepends=True):
        result.append((cursor, cursor + len(line)))
        cursor += len(line)
    if cursor != len(text):
        raise ValueError("snapshot line mapping is incomplete")
    return tuple(result)


def displayed_range(text: str, start: int, end: int) -> tuple[int, int]:
    """Exclude formatting stripped by the legacy chunker from evidence accounting."""
    raw = text[start:end]
    visible = raw.strip()
    if not visible:
        raise ValueError("gold label has no visible source text")
    offset = raw.index(visible)
    return start + offset, start + offset + len(visible)


def covers_all(selected: list[TrialUnit], gold: tuple[tuple[int, int], ...]) -> bool:
    ordered = sorted((unit.start, unit.end) for unit in selected)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return all(
        any(start <= need_start and end >= need_end for start, end in merged)
        for need_start, need_end in gold
    )


def choose(
    ranked: list[TrialUnit],
    *,
    budget: int = RENDER_BUDGET_TOKENS,
    token_counts: Mapping[str, int] | None = None,
) -> list[TrialUnit]:
    chosen: list[TrialUnit] = []
    used = 0
    for unit in ranked[:TOP_K]:
        cost = token_counts[unit.id] if token_counts is not None else len(unit.text)
        if used + cost <= budget:
            chosen.append(unit)
            used += cost
    return chosen


def rendered_token_counts(units: list[TrialUnit], tei_url: str) -> dict[str, int]:
    """The trial's pinned BGE tokenizer, including special tokens, at equal budget."""
    result: dict[str, int] = {}
    with httpx.Client(timeout=30.0) as client:
        for start in range(0, len(units), 4):
            window = units[start : start + 4]
            response = client.post(
                f"{tei_url.rstrip('/')}/tokenize",
                json={"inputs": [item.text for item in window], "truncate": False},
            )
            response.raise_for_status()
            raw: object = response.json()
            if not isinstance(raw, list):
                raise ValueError("tokenizer response does not partition trial segments")
            token_lists = cast(list[object], raw)
            if len(token_lists) != len(window):
                raise ValueError("tokenizer response does not partition trial segments")
            for item, tokens in zip(window, token_lists, strict=True):
                if not isinstance(tokens, list) or not tokens:
                    raise ValueError("tokenizer returned no tokens")
                result[item.id] = len(cast(list[object], tokens))
    return result


def cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    magnitude = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
    return numerator / magnitude if magnitude else 0.0


def rank(
    units: list[TrialUnit], vectors: list[list[float]], question: list[float]
) -> list[TrialUnit]:
    if len(units) != len(vectors):
        raise ValueError("trial vectors do not match segment identities")
    return [
        unit
        for _, unit in sorted(
            ((cosine(vector, question), unit) for unit, vector in zip(units, vectors, strict=True)),
            key=lambda pair: (-pair[0], pair[1].id),
        )
    ]


def run(snapshot: Path, *, tei_url: str) -> dict[str, object]:
    text = snapshot.read_text(encoding="utf-8")
    if hashlib.sha256(text.encode()).hexdigest() != SNAPSHOT_SHA256:
        raise ValueError("public snapshot changed; labels and model comparison are invalid")
    source = Source(
        SOURCE_ID, None, text, SNAPSHOT_SHA256, "zenith-eval-html-main-v1", "2026-09-26"
    )
    legacy: list[TrialUnit] = []
    for index, item in enumerate(chunk_stream(text)):
        start, end = displayed_range(text, item.char_start, item.char_end)
        if text[start:end] != item.text:
            raise ValueError("legacy chunk display text does not match source coordinates")
        legacy.append(TrialUnit(str(index), start, end, item.text))
    groups = [
        TrialUnit(str(item.id), item.start, item.end, item.text)
        for item in structural_groups(
            source, atomic_spans(source, max_chars=1000), target_chars=1200, max_chars=1600
        )
    ]
    if not legacy or not groups:
        raise ValueError("trial has no segments")
    embedder = TeiEmbedder(tei_url)
    started = time.perf_counter()
    token_counts = rendered_token_counts(legacy + groups, tei_url)
    tokenization_ms = round((time.perf_counter() - started) * 1000)
    started = time.perf_counter()
    legacy_vectors = embedder.encode([item.text for item in legacy], batch=4)
    legacy_embed_ms = round((time.perf_counter() - started) * 1000)
    started = time.perf_counter()
    group_vectors = embedder.encode([item.text for item in groups], batch=4)
    group_embed_ms = round((time.perf_counter() - started) * 1000)
    query_vectors = embedder.encode([query for _, query, _ in QUERIES], batch=4)
    lines = line_ranges(text)
    rows: list[dict[str, object]] = []
    for (name, query, line_numbers), query_vector in zip(QUERIES, query_vectors, strict=True):
        gold = tuple(displayed_range(text, *lines[number - 1]) for number in line_numbers)
        legacy_ranked = rank(legacy, legacy_vectors, query_vector)
        group_ranked = rank(groups, group_vectors, query_vector)
        # R1a keeps the legacy retrieval leaders fixed, then expands them to groups.
        leader_order = {item.id: position for position, item in enumerate(legacy_ranked)}
        grouped_from_legacy = sorted(
            groups,
            key=lambda group: (
                min(
                    (
                        leader_order[item.id]
                        for item in legacy
                        if item.start < group.end and item.end > group.start
                    ),
                    default=len(legacy),
                ),
                group.start,
            ),
        )
        outcomes: dict[str, object] = {}
        for route, ranked in (
            ("legacy", legacy_ranked),
            ("grouped_from_legacy", grouped_from_legacy),
            ("resegmented_reembedded", group_ranked),
        ):
            selected = choose(ranked, token_counts=token_counts)
            outcomes[route] = {
                "complete_gold_spans": covers_all(selected, gold),
                "top1_intersects_gold": any(
                    ranked[0].start < end and ranked[0].end > start for start, end in gold
                ),
                "rendered_characters": sum(len(item.text) for item in selected),
                "rendered_tokens": sum(token_counts[item.id] for item in selected),
                "selected_segment_ids": [item.id for item in selected],
            }
        rows.append(
            {"id": name, "question": query, "gold_lines": list(line_numbers), "routes": outcomes}
        )
    return {
        "trial_version": TRIAL_VERSION,
        "source_url": SOURCE_URL,
        "source_text_sha256": SNAPSHOT_SHA256,
        "label_provenance": "agent-reviewed exact extracted-line labels; exploratory",
        "embedding_model": EMBED_MODEL,
        "embedding_endpoint": tei_url,
        "segmentation_version": "zenith-structural-v1",
        "render_budget_tokens": RENDER_BUDGET_TOKENS,
        "tokenizer": EMBED_MODEL,
        "tokenization_ms": tokenization_ms,
        "top_k": TOP_K,
        "legacy_segments": len(legacy),
        "structural_segments": len(groups),
        "legacy_embedding_ms": legacy_embed_ms,
        "structural_embedding_ms": group_embed_ms,
        "queries": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--tei-url", default="http://127.0.0.1:18081")
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    report = run(options.snapshot, tei_url=options.tei_url)
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {options.output}")


if __name__ == "__main__":
    main()
