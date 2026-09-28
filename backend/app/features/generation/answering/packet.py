"""Bounded, source-mapped context bundles for an opt-in generation path.

Expansion reads ready chunks under the request's application-role RLS context.
The resulting packet is reauthorized by the caller before and after inference.
No model judgment grants source access or declares a suspected conflict true.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.core.database import tenant_session
from app.features.generation.answering import prompt
from app.features.retrieval.search import Hit
from app.features.tenancy.context import TenantContext

_PRIOR_CUE = re.compile(r"^\s*(?:except|unless|however|provided that|subject to)\b", re.I)
_NEXT_CUE = re.compile(r"^\s*(?:except|unless|however|provided that)\b", re.I)
_REFERENCE = re.compile(
    r"\b(?:defined in|subject to|see)\s+(section\s+\d+(?:\.\d+)*(?:\([a-z0-9]+\))?)",
    re.I,
)
_TABLE_ROW = re.compile(r"(?:^|\n)\s*\|[^\n]*\|", re.M)

REASON_PRIMARY = "direct_fact"
REASON_PRIOR = "applicability_condition"
REASON_NEXT = "exception"
REASON_TABLE = "table_header"
REASON_DEFINITION = "definition"
REASON_COUNTER = "possible_exception_applicability_unassessed"


@dataclass(frozen=True, slots=True)
class EvidencePacket:
    question: str
    hits: tuple[Hit, ...]
    reasons: tuple[str, ...]
    unresolved: tuple[str, ...]
    rendered_cost: int
    cost_unit: str
    counterevidence_ids: tuple[UUID, ...] = ()
    counterevidence_status: str = "disabled"

    @property
    def complete(self) -> bool:
        return not self.unresolved


def _rendered_upper_bound(question: str, thread: str, hits: list[Hit], reasons: list[str]) -> int:
    # The complete UTF-8 prompt plus system text is a conservative byte-level
    # token bound for ordinary byte-fallback tokenizers. It is not a model's
    # measured token count and is reported under that name, not as exact usage.
    return len(prompt.SYSTEM.encode()) + len(
        prompt.build(question, hits, thread, tuple(reasons)).encode()
    )


def _covered(hit: Hit, selected: list[Hit]) -> bool:
    return any(
        other.document_id == hit.document_id
        and other.page_num == hit.page_num
        and other.char_start <= hit.char_start
        and hit.char_end <= other.char_end
        for other in selected
    )


def section_heading_pattern(reference: str) -> re.Pattern[str]:
    """Match a section's own heading, not every textual mention of its number."""
    match = re.fullmatch(r"section\s+(\d+(?:\.\d+)*)(?:\(([a-z0-9]+)\))?", reference, re.I)
    if match is None:
        raise ValueError("unsupported section reference")
    number, subdivision = match.groups()
    exact = re.escape(number)
    ending = r"(\.(?!\d)|[ :\-]|$)" if "." not in number else r"([. :\-)]|$)"
    prefix = rf"(^|\n)[ \t]*(Section[ \t]+)?{exact}{ending}"
    if subdivision:
        return re.compile(
            prefix + rf"[^\n]{{0,150}}\r?\n[ \t]*\({re.escape(subdivision)}\)",
            re.I,
        )
    return re.compile(prefix, re.I)


def _hydrate(row: Any) -> Hit:
    return Hit(
        chunk_id=row.id,
        document_id=row.document_id,
        filename=row.filename,
        media_type=row.media_type,
        page_num=row.page_num,
        char_start=row.char_start,
        char_end=row.char_end,
        text=row.text,
        bboxes=list(row.bboxes or []),
        lexical_rank=None,
        dense_rank=None,
        score=0.0,
        label_ids=list(row.label_ids or []),
        source_sha256=row.sha256,
    )


async def _neighbor(context: TenantContext, hit: Hit, *, before: bool) -> Hit | None:
    direction = "c.char_start < :start" if before else "c.char_start > :start"
    ordering = "DESC" if before else "ASC"
    async with tenant_session(context) as session:
        row = (
            await session.execute(
                text(
                    "SELECT c.id, c.document_id, d.filename, d.media_type, c.page_num, "
                    "c.char_start, c.char_end, c.text, c.bboxes, d.label_ids, d.sha256 "
                    "FROM chunks c JOIN documents d ON d.id = c.document_id "
                    "WHERE d.status = 'ready' AND c.document_id = :document "
                    "AND c.page_num IS NOT DISTINCT FROM :page AND "
                    + direction
                    + f" ORDER BY c.char_start {ordering} LIMIT 1"
                ),
                {
                    "document": hit.document_id,
                    "page": hit.page_num,
                    "start": hit.char_start,
                },
            )
        ).first()
    if row is None and hit.page_num is not None:
        adjacent_page = hit.page_num - 1 if before else hit.page_num + 1
        async with tenant_session(context) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT c.id, c.document_id, d.filename, d.media_type, c.page_num, "
                        "c.char_start, c.char_end, c.text, c.bboxes, d.label_ids, d.sha256 "
                        "FROM chunks c JOIN documents d ON d.id = c.document_id "
                        "WHERE d.status = 'ready' AND c.document_id = :document "
                        "AND c.page_num = :page " + f"ORDER BY c.char_start {ordering} LIMIT 1"
                    ),
                    {"document": hit.document_id, "page": adjacent_page},
                )
            ).first()
    if row is None or row.sha256 != hit.source_sha256:
        return None
    return _hydrate(row)


async def _referenced(context: TenantContext, hit: Hit, reference: str) -> Hit | None:
    # Same-document only. A title-like reference is a proposal, never an
    # authorization shortcut or proof that the found clause applies.
    pattern = section_heading_pattern(reference)
    if pattern.search(hit.text):
        return hit
    async with tenant_session(context) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT c.id, c.document_id, d.filename, d.media_type, c.page_num, "
                    "c.char_start, c.char_end, c.text, c.bboxes, d.label_ids, d.sha256 "
                    "FROM chunks c JOIN documents d ON d.id = c.document_id "
                    "WHERE d.status = 'ready' AND c.document_id = :document "
                    "AND c.id <> :current AND c.text ~* :heading "
                    "ORDER BY c.page_num NULLS FIRST, c.char_start, c.id LIMIT 2"
                ),
                {
                    "document": hit.document_id,
                    "current": hit.chunk_id,
                    "heading": pattern.pattern,
                },
            )
        ).all()
    if len(rows) != 1 or rows[0].sha256 != hit.source_sha256:
        return None
    return _hydrate(rows[0])


async def _counter_candidates(
    context: TenantContext, hit: Hit, excluded: set[UUID], cap: int
) -> tuple[Hit, ...]:
    """Propose bounded same-document exceptions; do not infer applicability."""
    if cap < 1:
        return ()
    async with tenant_session(context) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT c.id, c.document_id, d.filename, d.media_type, c.page_num, "
                    "c.char_start, c.char_end, c.text, c.bboxes, d.label_ids, d.sha256 "
                    "FROM chunks c JOIN documents d ON d.id = c.document_id "
                    "WHERE d.status = 'ready' AND c.document_id = :document "
                    "AND NOT (c.id = ANY(:excluded)) "
                    "AND c.text ~* '\\m(except|unless|notwithstanding|however)\\M' "
                    "ORDER BY c.page_num NULLS FIRST, c.char_start, c.id LIMIT :cap"
                ),
                {"document": hit.document_id, "excluded": list(excluded), "cap": cap},
            )
        ).all()
    return tuple(_hydrate(row) for row in rows if row.sha256 == hit.source_sha256)


async def build_packet(
    context: TenantContext,
    question: str,
    ranked: list[Hit],
    thread: str = "",
    *,
    token_budget: int = 16_000,
    max_extra: int = 8,
    counterevidence: bool = False,
    max_counterevidence: int = 2,
    render_cost: Callable[[list[Hit], list[str]], int] | None = None,
    cost_unit: str = "utf8_byte_bound",
) -> EvidencePacket:
    """Select complete local bundles; omit a fact if its known context is missing.

    Each extra is currently one same-page neighbor or one explicit same-document
    section reference. Long-range semantic dependencies without a clear reference
    remain unresolved rather than receiving a fabricated closure claim.
    """
    if token_budget < 1 or max_extra < 0 or max_counterevidence < 0 or not cost_unit:
        raise ValueError("invalid packet budget")

    def cost(hits: list[Hit], reasons: list[str]) -> int:
        if render_cost is not None:
            return render_cost(hits, reasons)
        return _rendered_upper_bound(question, thread, hits, reasons)

    selected: list[Hit] = []
    reasons: list[str] = []
    seen: set[UUID] = set()
    unresolved: list[str] = []
    extra_count = 0
    for primary in ranked:
        bundle: list[tuple[Hit, str]] = [(primary, REASON_PRIMARY)]
        missing = False
        prior_needed = bool(_PRIOR_CUE.match(primary.text) or _TABLE_ROW.search(primary.text))
        dependency_known = prior_needed
        previous = await _neighbor(context, primary, before=True) if prior_needed else None
        if prior_needed:
            if previous is None:
                missing = True
            else:
                bundle.append(
                    (previous, REASON_TABLE if _TABLE_ROW.search(primary.text) else REASON_PRIOR)
                )
        following = await _neighbor(context, primary, before=False)
        if following is not None and _NEXT_CUE.match(following.text):
            dependency_known = True
            bundle.append((following, REASON_NEXT))
        references = tuple(dict.fromkeys(_REFERENCE.findall(primary.text)))
        dependency_known = dependency_known or bool(references)
        for reference in references[:2]:
            definition = await _referenced(context, primary, reference)
            if definition is None:
                missing = True
            else:
                bundle.append((definition, REASON_DEFINITION))
        if len(references) > 2:
            missing = True
        bundled: set[UUID] = set()
        novel: list[tuple[Hit, str]] = []
        for hit, reason in bundle:
            if (
                hit.chunk_id not in seen
                and hit.chunk_id not in bundled
                and not _covered(hit, selected)
            ):
                novel.append((hit, reason))
                bundled.add(hit.chunk_id)
        extras = sum(hit.chunk_id != primary.chunk_id for hit, _ in novel)
        if extra_count + extras > max_extra:
            missing = True
        proposed = selected + [hit for hit, _ in novel]
        proposed_reasons = reasons + [reason for _, reason in novel]
        if cost(proposed, proposed_reasons) > token_budget:
            missing = True
        if missing:
            if dependency_known:
                unresolved.append("required_context_unavailable")
            continue
        for hit, reason in novel:
            selected.append(hit)
            reasons.append(reason)
            seen.add(hit.chunk_id)
        extra_count += extras
    counter_ids: list[UUID] = []
    counter_status = "disabled"
    if counterevidence:
        counter_status = "bounded_scan_complete" if max_counterevidence else "skipped_budget"
        checked_documents: set[UUID] = set()
        selected_documents = {hit.document_id for hit in selected}
        for primary in ranked:
            if (
                primary.document_id in checked_documents
                or primary.document_id not in selected_documents
            ):
                continue
            checked_documents.add(primary.document_id)
            candidates = await _counter_candidates(
                context, primary, seen, max_counterevidence - len(counter_ids)
            )
            for candidate in candidates:
                if candidate.chunk_id in seen or _covered(candidate, selected):
                    continue
                proposed = selected + [candidate]
                proposed_reasons = reasons + [REASON_COUNTER]
                if cost(proposed, proposed_reasons) > token_budget:
                    counter_status = "partial"
                    continue
                selected.append(candidate)
                reasons.append(REASON_COUNTER)
                seen.add(candidate.chunk_id)
                counter_ids.append(candidate.chunk_id)
            if len(counter_ids) >= max_counterevidence:
                break
    return EvidencePacket(
        question,
        tuple(selected),
        tuple(reasons),
        tuple(dict.fromkeys(unresolved)),
        cost(selected, reasons),
        cost_unit,
        tuple(counter_ids),
        counter_status,
    )
