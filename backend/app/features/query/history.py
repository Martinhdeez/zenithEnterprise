"""Past questions and the answers they got.

`queries` and `query_citations` have been written since F8 as observability and as the seed
of the audit trail iteration 4 needs. This reads them back.

**Privacy here is enforced by Postgres, not by this module.** F15 shipped it as a `WHERE`
clause in Python and said so loudly, because it was the one place in this system where
application code did security work: RLS modelled tenant and label, and neither expresses
"my rows versus my colleagues'".

Migration 0005 closed that. `zenith.user_id` and `zenith.reads_all_history` join the
context, and the policy on `queries` reads:

    tenant_id = zenith_current_tenant()
    AND (zenith_reads_all_history() OR user_id = zenith_current_user_id())

So the filter below is gone. What remains is the *permission* decision — whether this
caller reads everyone's history — which is resolved from the catalogue the application
already owns and handed to the database as a boolean. Asking Postgres to re-derive
authority from permission strings would put the catalogue in two places that can disagree.

The questions people ask are more revealing than the documents they read — *"what is my
severance?"*, *"can I be dismissed for this?"* — which is why it was worth a third context
variable rather than a comment asking future maintainers to be careful.
"""

from dataclasses import dataclass, replace
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
        #
        # **No `user_id` condition.** The policy applies it, from the context bound below.
        conditions: list[str] = []
        if after:
            conditions.append("(q.created_at, q.id) < (:after_at, :after_id)")
        where = f"WHERE {' AND '.join(conditions)} " if conditions else ""

        scoped = replace(
            self.profile.context,
            user_id=self.profile.user_id,
            reads_all_history=everyones,
        )

        async with tenant_session(scoped) as session:
            rows = await session.execute(
                text(
                    "SELECT q.id, q.question, q.answer, q.model_used, q.user_id, "
                    "q.latency_retrieval_ms, q.latency_generation_ms, q.created_at, "
                    "(SELECT count(*) FROM query_citations c WHERE c.query_id = q.id) AS citations "
                    "FROM queries q "
                    f"{where}"
                    "ORDER BY q.created_at DESC, q.id DESC LIMIT :limit"
                ),
                {
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
