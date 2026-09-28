"""Offline, source-preserving segmentation trial; the active chunk index is untouched.

Offsets refer to a stored page's extracted text (or a non-paginated text stream),
including whitespace. Existing ingestion chunks trim their display text, so their
text cannot be concatenated as a lossless surrogate for that representation.
"""

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from uuid import NAMESPACE_URL, UUID, uuid5

SEGMENTATION_VERSION = "zenith-structural-v1"
_HEADING = re.compile(r"^(?:#{1,6}\s+|(?:section|sección|chapter|capítulo)\s+\S+)", re.I)
_LIST = re.compile(r"^\s*(?:[-*•]\s+|\(?\d+(?:\.\d+)*[.)]\s+)")


class Kind(StrEnum):
    HEADING = "heading"
    LIST = "list"
    TABLE = "table"
    CODE = "code"
    BLANK = "blank"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class Source:
    document_id: UUID
    page_num: int | None
    text: str
    source_sha256: str
    parser_version: str
    extraction_version: str

    @property
    def identity(self) -> str:
        return hashlib.sha256(
            "\0".join(
                (
                    str(self.document_id),
                    str(self.page_num),
                    self.source_sha256,
                    self.parser_version,
                    self.extraction_version,
                    hashlib.sha256(self.text.encode()).hexdigest(),
                )
            ).encode()
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class Span:
    id: UUID
    source_identity: str
    start: int
    end: int
    text: str
    kind: Kind
    forced_split: bool = False


@dataclass(frozen=True, slots=True)
class Group:
    id: UUID
    source_identity: str
    start: int
    end: int
    members: tuple[UUID, ...]
    text: str
    forced_splits: int


@dataclass(frozen=True, slots=True)
class BoundaryAssessment:
    boundary_id: UUID
    source_identity: str
    probability: float | None
    status: str  # assessed | unavailable | malformed
    provider: str
    model: str | None
    rubric_id: str | None
    input_fingerprint: str | None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"assessed", "unavailable", "malformed"}:
            raise ValueError("invalid boundary status")
        if self.status == "assessed":
            if (
                self.probability is None
                or not isfinite(self.probability)
                or not 0 <= self.probability <= 1
            ):
                raise ValueError("invalid boundary probability")
        elif self.probability is not None:
            raise ValueError("failed boundary cannot have a probability")


def boundary_id(left: Span, right: Span) -> UUID:
    if left.source_identity != right.source_identity or left.end != right.start:
        raise ValueError("boundary spans are not adjacent in one source")
    return uuid5(NAMESPACE_URL, f"{left.source_identity}:{left.id}:{right.id}")


def atomic_spans(source: Source, *, max_chars: int = 1600) -> tuple[Span, ...]:
    """Split at extracted line boundaries, retaining every codepoint and line ending.

    Line kinds are heuristics because the current parsers persist text, not a
    heading/list/table/code tree. An oversized line is force-split with an explicit mark.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    spans: list[Span] = []
    cursor = 0
    in_code = False
    source_identity = source.identity
    for line in source.text.splitlines(keepends=True):
        content = line.strip()
        if content.startswith("```"):
            kind = Kind.CODE
            in_code = not in_code
        elif in_code:
            kind = Kind.CODE
        elif not content:
            kind = Kind.BLANK
        elif _HEADING.match(content):
            kind = Kind.HEADING
        elif _LIST.match(line):
            kind = Kind.LIST
        elif "|" in line and line.count("|") >= 2:
            kind = Kind.TABLE
        else:
            kind = Kind.TEXT
        for offset in range(0, len(line), max_chars):
            piece = line[offset : offset + max_chars]
            start = cursor + offset
            end = start + len(piece)
            spans.append(
                Span(
                    uuid5(
                        NAMESPACE_URL,
                        f"{source_identity}:{start}:{end}:{hashlib.sha256(piece.encode()).hexdigest()}",
                    ),
                    source_identity,
                    start,
                    end,
                    piece,
                    kind,
                    len(line) > max_chars,
                )
            )
        cursor += len(line)
    if cursor != len(source.text):
        raise AssertionError("source line iterator changed offsets")
    validate_spans(source, tuple(spans))
    return tuple(spans)


def validate_spans(source: Source, spans: tuple[Span, ...]) -> None:
    cursor = 0
    source_identity = source.identity
    for span in spans:
        if (
            span.source_identity != source_identity
            or span.start != cursor
            or span.end <= span.start
            or span.text != source.text[span.start : span.end]
        ):
            raise ValueError("span coverage or source mapping mismatch")
        cursor = span.end
    if cursor != len(source.text):
        raise ValueError("unmapped extracted source content")


def structural_groups(
    source: Source,
    spans: tuple[Span, ...],
    *,
    target_chars: int = 1200,
    max_chars: int = 1600,
    assessments: tuple[BoundaryAssessment, ...] = (),
) -> tuple[Group, ...]:
    """Bounded greedy selector; optional assessed boundaries only change cut preference."""
    if not 0 < target_chars <= max_chars:
        raise ValueError("invalid group size")
    validate_spans(source, spans)
    source_identity = source.identity
    by_boundary = {item.boundary_id: item for item in assessments}
    if len(by_boundary) != len(assessments):
        raise ValueError("duplicate boundary assessment")
    available = {boundary_id(left, right) for left, right in zip(spans, spans[1:], strict=False)}
    if any(item.source_identity != source_identity for item in assessments) or not set(
        by_boundary
    ).issubset(available):
        raise ValueError("boundary does not belong to this source version")
    groups: list[Group] = []
    index = 0
    while index < len(spans):
        choices: list[tuple[float, int]] = []
        for end in range(index + 1, len(spans) + 1):
            size = spans[end - 1].end - spans[index].start
            if size > max_chars:
                break
            if end == len(spans):
                choices.append((abs(size - target_chars), end))
                continue
            left, right = spans[end - 1], spans[end]
            assessment = by_boundary.get(boundary_id(left, right))
            probability = (
                assessment.probability
                if assessment is not None and assessment.status == "assessed"
                else None
            )
            bonus = target_chars * 0.15 * probability if probability is not None else 0.0
            if right.kind is Kind.HEADING or left.kind is Kind.BLANK:
                bonus += target_chars * 0.12
            if left.kind is Kind.HEADING:
                bonus -= target_chars * 0.4  # keep a heading with its first content
            choices.append((abs(size - target_chars) - bonus, end))
        if not choices:
            raise ValueError("atomic span exceeds group limit; split with a smaller atomic bound")
        _, end = min(choices, key=lambda choice: (choice[0], choice[1]))
        members = spans[index:end]
        start_offset = members[0].start
        end_offset = members[-1].end
        member_ids = tuple(item.id for item in members)
        groups.append(
            Group(
                uuid5(NAMESPACE_URL, f"{SEGMENTATION_VERSION}:{source_identity}:{member_ids}"),
                source_identity,
                start_offset,
                end_offset,
                member_ids,
                source.text[start_offset:end_offset],
                sum(item.forced_split for item in members),
            )
        )
        index = end
    validate_groups(source, tuple(groups))
    return tuple(groups)


def validate_groups(source: Source, groups: tuple[Group, ...]) -> None:
    cursor = 0
    source_identity = source.identity
    for group in groups:
        if (
            group.source_identity != source_identity
            or group.start != cursor
            or group.end <= group.start
            or group.text != source.text[group.start : group.end]
        ):
            raise ValueError("group coverage or source mapping mismatch")
        cursor = group.end
    if cursor != len(source.text):
        raise ValueError("unmapped grouped source content")


def overlapping_chunk_ids(
    group: Group, chunks: tuple[tuple[UUID, int, int], ...]
) -> tuple[UUID, ...]:
    """Map legacy chunk coordinates to a group; overlap is not unique coverage."""
    return tuple(
        chunk_id for chunk_id, start, end in chunks if start < group.end and end > group.start
    )
