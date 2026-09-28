"""Source-coordinate evidence scoring shared by independent evaluation runners.

No optional segmentation or packet implementation is imported here. These
helpers consume fixed source intervals and local model vectors only.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

import httpx

RENDER_BUDGET_TOKENS = 1100
TOP_K = 8
EMBED_MODEL = "BAAI/bge-m3@5617a9f61b028005a4858fdac845db406aefb181"


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
