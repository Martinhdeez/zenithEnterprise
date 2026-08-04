"""Past questions and the answers they got.

`queries` and `query_citations` have been written since F8 as observability and as the seed
of the audit trail iteration 4 needs. This reads them back.

**One filter here is application code doing security work, which this project normally
refuses.** RLS gives two levels — tenant and label — and neither models "your own rows
versus your colleagues'". `queries` is tenant-scoped by policy, so a member holding only
`query.history.own` would see every question their colleagues asked if this module did not
filter by `user_id` itself.

That is worth being uncomfortable about, and worth stating rather than burying: the
questions people ask are more revealing than the documents they read. *"What is my
severance?"*, *"can I be dismissed for this?"* — a history endpoint that leaked those would
be a worse breach than the corpus, and it would look like a working feature. The filter is
therefore applied in one place, with the permission decided by the caller's profile rather
than by anything in the request, and a test asserts a member cannot read another member's
history.

Upgrading `queries` to a row-level policy on `user_id` would be the better answer and is
recorded as such in the F15 write-up: it needs a third RLS context variable, which is a
schema change with its own migration and its own blast radius.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text

from app.common.exceptions import PermissionDeniedError
from app.core.database import tenant_session
from app.features.auth.service import AccessProfile
from app.features.documents.pagination import Cursor, clamp

OWN = "query.history.own"
ANY = "query.history.any"


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    query_id: UUID
    question: str
    answer: str | None
    model_used: str | None
    citations: int
    latency_retrieval_ms: int | None
    latency_generation_ms: int | None
    created_at: datetime
    #: Whether this was the caller's own question. A shared history is only readable if you
    #: can tell whose it was.
    mine: bool


@dataclass(frozen=True, slots=True)
class HistoryPage:
    entries: list[HistoryEntry]
    next_cursor: str | None


class HistoryService:
    def __init__(self, profile: AccessProfile) -> None:
        self.profile = profile

    async def page(self, limit: int | None = None, cursor: str | None = None) -> HistoryPage:
        if not {OWN, ANY} & self.profile.permissions:
            raise PermissionDeniedError("you may not read query history")

        # Decided from the profile, never from a parameter. A `scope=all` query string
        # would be a request the client makes; this is a fact about the caller.
        everyones = ANY in self.profile.permissions
        size = clamp(limit)
        after = Cursor.decode(cursor) if cursor else None

        # Keyset, for the same reason F4 chose it for documents: `OFFSET n` evaluates the
        # policy on every discarded row, and offsets repeat or skip rows whenever something
        # is written between two pages — which, for a log written by every query, is always.
        conditions = ["(:everyones OR q.user_id = :user_id)"]
        if after:
            conditions.append("(q.created_at, q.id) < (:after_at, :after_id)")

        async with tenant_session(self.profile.context) as session:
            rows = await session.execute(
                text(
                    "SELECT q.id, q.question, q.answer, q.model_used, q.user_id, "
                    "q.latency_retrieval_ms, q.latency_generation_ms, q.created_at, "
                    "(SELECT count(*) FROM query_citations c WHERE c.query_id = q.id) AS citations "
                    "FROM queries q "
                    f"WHERE {' AND '.join(conditions)} "
                    "ORDER BY q.created_at DESC, q.id DESC LIMIT :limit"
                ),
                {
                    "everyones": everyones,
                    "user_id": self.profile.user_id,
                    "limit": size + 1,
                    **({"after_at": after.created_at, "after_id": after.id} if after else {}),
                },
            )
            found = list(rows)

        # One extra row is fetched to know whether another page exists, and dropped before
        # returning. Counting instead would be a second query over the whole log.
        more = len(found) > size
        entries = [
            HistoryEntry(
                query_id=row.id,
                question=row.question,
                answer=row.answer,
                model_used=row.model_used,
                citations=int(row.citations),
                latency_retrieval_ms=row.latency_retrieval_ms,
                latency_generation_ms=row.latency_generation_ms,
                created_at=row.created_at,
                mine=row.user_id == self.profile.user_id,
            )
            for row in found[:size]
        ]
        last = found[size - 1] if more else None
        return HistoryPage(
            entries=entries,
            next_cursor=Cursor(last.created_at, last.id).encode() if last else None,
        )
