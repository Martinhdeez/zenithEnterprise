"""Bounded, source-preserving manifest planning before query embedding."""

import hashlib
import hmac
import math
from dataclasses import dataclass
from uuid import UUID, uuid5

from sqlalchemy import text

from app.core.config import settings
from app.core.database import tenant_session
from app.features.auth.service import AccessProfile, AuthService
from app.features.retrieval.search import Hit
from app.features.tenancy.context import TenantContext

WINDOW_NAMESPACE = UUID("e5728348-4192-49a2-883b-b67624512d50")


@dataclass(frozen=True, slots=True)
class Window:
    id: UUID
    chunk_id: UUID
    start: int  # Half-open Python-codepoint offset within the stored chunk text.
    end: int
    text: str


@dataclass(frozen=True, slots=True)
class DirectPlan:
    complete: bool
    reason: str | None
    units: tuple[Hit, ...] = ()
    windows: tuple[Window, ...] = ()
    eligible_units: int | None = None
    manifest_fingerprint: str | None = None


async def verify_current(
    profile: AccessProfile, context: TenantContext, units: tuple[Hit, ...]
) -> bool:
    """Re-resolve user authority and every constituent under RLS in a short read."""
    try:
        current = await AuthService().profile(profile.user_id, context.tenant_id)
        if "query.execute" not in current.permissions:
            return False
        if not units:
            return True
        labels = set(context.label_ids).intersection(current.context.label_ids)
        narrowed = TenantContext.for_tenant(context.tenant_id, labels)
        async with tenant_session(narrowed) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT c.id, c.document_id, c.text, d.sha256 "
                        "FROM chunks c JOIN documents d ON d.id = c.document_id "
                        "WHERE d.status = 'ready' AND c.id = ANY(:ids)"
                    ),
                    {"ids": [hit.chunk_id for hit in units]},
                )
            ).all()
        by_id = {row.id: row for row in rows}
        return len(by_id) == len(units) and all(
            (row := by_id.get(hit.chunk_id)) is not None
            and row.document_id == hit.document_id
            and row.text == hit.text
            and hit.source_sha256 is not None
            and row.sha256 == hit.source_sha256
            for hit in units
        )
    except Exception:  # noqa: BLE001 - failed authorization cannot permit disclosure
        return False


def fingerprint(parts: list[str]) -> str:
    """Opaque for clients; neither private queries nor enumerable IDs are logged."""
    return hmac.new(
        settings.jwt_secret.encode(), "\0".join(parts).encode(), hashlib.sha256
    ).hexdigest()


def make_windows(hit: Hit, size: int, overlap: int, max_window_bytes: int) -> tuple[Window, ...]:
    if size < 1 or not 0 <= overlap < size:
        raise ValueError("invalid direct window size")
    if not hit.source_sha256:
        raise ValueError("direct source has no version identity")
    result: list[Window] = []
    start = 0
    while start < len(hit.text):
        end = min(len(hit.text), start + size)
        while end > start and len(hit.text[start:end].encode()) > max_window_bytes:
            end -= 1
        if end == start:
            raise ValueError("a source codepoint exceeds the direct pair budget")
        content = hit.text[start:end]
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        identity = f"{hit.chunk_id}:{hit.source_sha256}:{start}:{end}:{content_hash}"
        result.append(Window(uuid5(WINDOW_NAMESPACE, identity), hit.chunk_id, start, end, content))
        if end == len(hit.text):
            break
        start = end - min(overlap, end - start - 1)
    return tuple(result)


async def plan(
    context: TenantContext,
    documents: list[UUID] | None,
    question: str,
    *,
    max_units: int,
    max_windows: int,
    max_source_bytes: int,
    max_rendered_bytes: int,
    window_chars: int,
    overlap_chars: int,
    max_pair_bytes: int = 500,
) -> DirectPlan:
    """Read at most cap+1 ready units under RLS; hydrate only a bounded scope."""
    if (
        min(
            max_units,
            max_windows,
            max_source_bytes,
            max_rendered_bytes,
            window_chars,
            max_pair_bytes,
        )
        < 1
    ):
        raise ValueError("invalid direct budget")
    if not 0 <= overlap_chars < window_chars:
        raise ValueError("invalid direct overlap")
    # Keep rendered pairs small before dispatch. Tokenizer normalization may expand
    # characters, so the local direct TEI call also explicitly disables truncation.
    max_window_bytes = max_pair_bytes - len(question.encode())
    if max_window_bytes < 1:
        return DirectPlan(False, "direct_pair_budget")
    scope_sql = " AND c.document_id = ANY(:documents)" if documents else ""
    params: dict[str, object] = {"cap": max_units + 1}
    if documents:
        params["documents"] = documents
    async with tenant_session(context) as session:
        manifest = (
            await session.execute(
                text(
                    "SELECT c.id, c.document_id, d.sha256, "
                    "length(c.text) AS chars, octet_length(c.text) AS bytes "
                    "FROM chunks c JOIN documents d ON d.id = c.document_id "
                    "WHERE d.status = 'ready' AND c.text <> ''"
                    + scope_sql
                    + " ORDER BY c.id LIMIT :cap"
                ),
                params,
            )
        ).all()
        if len(manifest) > max_units:
            return DirectPlan(False, "direct_unit_limit")
        if not manifest:
            return DirectPlan(True, None, eligible_units=0, manifest_fingerprint=fingerprint([]))
        # Count windows before transferring text. A single huge unit cannot silently
        # consume an unbounded response, even when the manifest has one row.
        estimated_windows = sum(
            max(1, math.ceil((int(row.chars) - overlap_chars) / (window_chars - overlap_chars)))
            for row in manifest
        )
        if estimated_windows > max_windows:
            return DirectPlan(False, "direct_window_limit", eligible_units=len(manifest))
        if sum(int(row.bytes) for row in manifest) > max_source_bytes:
            return DirectPlan(False, "direct_source_budget", eligible_units=len(manifest))
        source_rows = (
            await session.execute(
                text(
                    "SELECT c.id, c.document_id, d.sha256, d.filename, d.media_type, "
                    "c.page_num, c.char_start, c.char_end, c.text, c.bboxes, d.label_ids "
                    "FROM chunks c JOIN documents d ON d.id = c.document_id "
                    "WHERE d.status = 'ready' AND c.text <> '' AND c.id = ANY(:ids)"
                ),
                {"ids": [row.id for row in manifest]},
            )
        ).all()
    by_id = {row.id: row for row in source_rows}
    if len(by_id) != len(manifest):
        return DirectPlan(False, "direct_snapshot_changed", eligible_units=len(manifest))
    units: list[Hit] = []
    for meta in manifest:
        row = by_id[meta.id]
        if (
            row.sha256 != meta.sha256
            or row.document_id != meta.document_id
            or len(row.text) != meta.chars
            or len(row.text.encode()) != meta.bytes
        ):
            return DirectPlan(False, "direct_snapshot_changed", eligible_units=len(manifest))
        units.append(
            Hit(
                chunk_id=row.id,
                document_id=row.document_id,
                filename=row.filename,
                media_type=row.media_type,
                page_num=row.page_num,
                char_start=row.char_start,
                char_end=row.char_end,
                text=row.text,
                bboxes=list(row.bboxes or []),
                label_ids=list(row.label_ids or []),
                lexical_rank=None,
                dense_rank=None,
                score=0.0,
                source_sha256=row.sha256,
            )
        )
    try:
        windows = tuple(
            window
            for hit in units
            for window in make_windows(hit, window_chars, overlap_chars, max_window_bytes)
        )
    except ValueError:
        return DirectPlan(False, "direct_pair_budget", eligible_units=len(manifest))
    if len(windows) > max_windows:
        return DirectPlan(False, "direct_window_limit", eligible_units=len(manifest))
    rendered_bytes = sum(
        len(window.text.encode()) + len(question.encode()) + 4096 for window in windows
    )
    if rendered_bytes > max_rendered_bytes:
        return DirectPlan(False, "direct_render_budget", eligible_units=len(manifest))
    identity = [
        f"{hit.chunk_id}:{hit.source_sha256}:{hashlib.sha256(hit.text.encode()).hexdigest()}"
        for hit in units
    ]
    return DirectPlan(
        True,
        None,
        tuple(units),
        windows,
        len(units),
        fingerprint(identity),
    )
