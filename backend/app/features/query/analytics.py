"""What the installation has been asked, who asked it, and what it read to answer.

Almost none of this is new data. `queries` has logged the question, the answer, the model
and both latencies since 0001, and `query_citations` has recorded every passage an answer
leaned on with all four of its scores. The tables were built for exactly this and nothing
had ever read them. Adding a second audit log beside them would have meant two records of
the same event, drifting.

**Scoped by the same policy the history screen uses, not by a `WHERE` clause here.**
Migration 0005 put `zenith.user_id` and `zenith.reads_all_history` into the `queries`
policy: without `query.history.any` a caller sees only their own rows, and that holds
whether the reader is the history list or this. So an administrator without that permission
gets analytics of their own activity rather than an error — which is the honest answer, and
one nobody can widen by editing a query in this file.

Aggregates are computed in Postgres rather than by reading rows into Python. A tenant with a
year of questions is tens of thousands of rows, and "count them in the application" is the
kind of thing that works for the whole of the first year.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from uuid import UUID

from sqlalchemy import text

from app.core.database import tenant_session
from app.features.auth.service import AccessProfile
from app.features.documents.pagination import Cursor, clamp
from app.features.query.history import ANY

#: How far back the dashboard looks. Not configurable yet, and a fixed window is the honest
#: default: every number on the screen means the same thing as every other one, which stops
#: being true the moment two of them answer different questions about different periods.
WINDOW_DAYS = 30

#: Rows per page of the audit log. Ten rather than fifty, and the reason is the screen
#: rather than the query: fifty rows push every other panel on the admin page far below the
#: fold, so an administrator scrolls past a wall of questions to reach the access matrix.
AUDIT_PAGE = 10


@dataclass(frozen=True, slots=True)
class Totals:
    queries: int
    #: Distinct people who asked something in the window.
    users: int
    #: Sum over the rows that reported any, and the count of rows that did not. Kept apart
    #: rather than summed with zeros: an unknown cost and a zero cost are different answers
    #: and only one of them is ever true. See `GenerationResponse`.
    prompt_tokens: int
    completion_tokens: int
    queries_without_usage: int
    #: Milliseconds, averaged. Retrieval and generation separately, because they fail for
    #: unrelated reasons and an average of the two hides which one is slow.
    average_retrieval_ms: int
    average_generation_ms: int
    #: Answers that found nothing to cite. The most useful number here: a corpus that
    #: abstains often is a corpus missing something people keep asking for.
    abstentions: int


@dataclass(frozen=True, slots=True)
class ActiveUser:
    user_id: UUID | None
    email: str | None
    queries: int


@dataclass(frozen=True, slots=True)
class CitedDocument:
    document_id: UUID
    filename: str
    #: How many answers leaned on it, not how many passages were cited — a document quoted
    #: four times in one answer is one answer.
    answers: int


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One question, and the documents the system opened to answer it."""

    query_id: UUID
    asked_at: datetime
    email: str | None
    question: str
    abstained: bool
    model: str | None
    latency_ms: int
    #: Filenames, deduplicated. The point of the row: what an answer *read* is the part an
    #: auditor asks about, and it is not derivable from the answer text.
    documents: list[str] = field(default_factory=list[str])


@dataclass(frozen=True, slots=True)
class Analytics:
    """The aggregates. The audit log is `AuditPage`, fetched separately.

    Split because they answer different questions and change at different rates: the
    aggregates are one row of numbers that a page turn cannot alter, and recomputing four
    of them to hand back ten different log rows is work nobody asked for.
    """

    window_days: int
    totals: Totals
    most_active: list[ActiveUser]
    top_cited: list[CitedDocument]


@dataclass(frozen=True, slots=True)
class AuditPage:
    entries: list[AuditEntry]
    #: Opaque. Pass it back as `cursor`; `None` means this is the last page.
    next_cursor: str | None


class AnalyticsService:
    def __init__(self, profile: AccessProfile) -> None:
        self.profile = profile
        # Bound the same way `HistoryService` binds itself. Without `query.history.any` the
        # policy narrows every statement below to this caller's own rows, in the database,
        # which is why none of them carry a user filter.
        self.context = replace(
            profile.context,
            user_id=profile.user_id,
            reads_all_history=ANY in profile.permissions,
        )

    async def overview(self) -> Analytics:
        since = f"now() - interval '{WINDOW_DAYS} days'"

        async with tenant_session(self.context) as session:
            totals = (
                await session.execute(
                    text(
                        "SELECT count(*) AS queries, "
                        "  count(DISTINCT user_id) AS users, "
                        "  coalesce(sum(prompt_tokens), 0) AS prompt_tokens, "
                        "  coalesce(sum(completion_tokens), 0) AS completion_tokens, "
                        "  count(*) FILTER (WHERE prompt_tokens IS NULL) AS no_usage, "
                        "  coalesce(round(avg(latency_retrieval_ms)), 0) AS retrieval_ms, "
                        "  coalesce(round(avg(latency_generation_ms)), 0) AS generation_ms, "
                        # An abstention is a query with no citations. Derived rather than
                        # stored: `queries` has no `abstained` column, and the citation rows
                        # are the same evidence the answer was bound against.
                        "  count(*) FILTER (WHERE NOT EXISTS ("
                        "    SELECT 1 FROM query_citations c WHERE c.query_id = q.id"
                        "  )) AS abstentions "
                        f"FROM queries q WHERE q.created_at >= {since}"
                    )
                )
            ).one()

            active = await session.execute(
                text(
                    "SELECT q.user_id, u.email, count(*) AS queries "
                    "FROM queries q LEFT JOIN users u ON u.id = q.user_id "
                    f"WHERE q.created_at >= {since} "
                    "GROUP BY q.user_id, u.email ORDER BY count(*) DESC, u.email LIMIT 10"
                )
            )

            cited = await session.execute(
                text(
                    # `count(DISTINCT c.query_id)`, not `count(*)`: a document quoted four
                    # times in one answer is one answer, and counting citations would rank
                    # long documents above useful ones.
                    "SELECT d.id, d.filename, count(DISTINCT c.query_id) AS answers "
                    "FROM query_citations c "
                    "JOIN queries q ON q.id = c.query_id "
                    "JOIN chunks ch ON ch.id = c.chunk_id "
                    "JOIN documents d ON d.id = ch.document_id "
                    f"WHERE q.created_at >= {since} "
                    "GROUP BY d.id, d.filename ORDER BY count(DISTINCT c.query_id) DESC LIMIT 10"
                )
            )

            return Analytics(
                window_days=WINDOW_DAYS,
                totals=Totals(
                    queries=int(totals.queries),
                    users=int(totals.users),
                    prompt_tokens=int(totals.prompt_tokens),
                    completion_tokens=int(totals.completion_tokens),
                    queries_without_usage=int(totals.no_usage),
                    average_retrieval_ms=int(totals.retrieval_ms),
                    average_generation_ms=int(totals.generation_ms),
                    abstentions=int(totals.abstentions),
                ),
                most_active=[
                    ActiveUser(user_id=row.user_id, email=row.email, queries=int(row.queries))
                    for row in active
                ],
                top_cited=[
                    CitedDocument(
                        document_id=row.id, filename=row.filename, answers=int(row.answers)
                    )
                    for row in cited
                ],
            )

    async def audit(self, cursor: Cursor | None = None, limit: int | None = None) -> AuditPage:
        """One page of the log, newest first, resumed from a cursor rather than an offset.

        `OFFSET` would make Postgres produce and discard every row before this page — and
        under RLS the policy is evaluated on each of those discarded rows too, so page ten
        costs ten pages of access checks nobody sees. `documents/pagination.Cursor` already
        encodes `(created_at, id)`, which is exactly the key this needs and exactly the index
        migration 0013 added.

        `id` is in the key rather than the timestamp alone because `created_at` is not
        unique: two questions asked in the same millisecond would make a timestamp-only
        cursor skip one of them or repeat it forever. `LabelCursor` learned that the hard
        way when migration 0007 backfilled every label with one timestamp.
        """
        size = clamp(limit) if limit is not None else AUDIT_PAGE
        since = f"now() - interval '{WINDOW_DAYS} days'"
        # Strictly older than the last row of the previous page, in the same ordering. The
        # row comparison is what makes it one index seek rather than a scan with a filter.
        after = "AND (q.created_at, q.id) < (:cursor_at, :cursor_id) " if cursor else ""
        parameters = {"cursor_at": cursor.created_at, "cursor_id": cursor.id} if cursor else {}

        async with tenant_session(self.context) as session:
            rows = list(
                await session.execute(
                    text(
                        "SELECT q.id, q.created_at, u.email, q.question, q.model_used, "
                        "  coalesce(q.latency_retrieval_ms, 0) + "
                        "  coalesce(q.latency_generation_ms, 0) AS latency_ms, "
                        # Aggregated in the same pass rather than a query per row: ten rows
                        # would otherwise be ten round trips, on a screen that is refreshed.
                        "  coalesce(array_agg(DISTINCT d.filename) "
                        "    FILTER (WHERE d.filename IS NOT NULL), '{}') AS documents "
                        "FROM queries q "
                        "LEFT JOIN users u ON u.id = q.user_id "
                        "LEFT JOIN query_citations c ON c.query_id = q.id "
                        "LEFT JOIN chunks ch ON ch.id = c.chunk_id "
                        "LEFT JOIN documents d ON d.id = ch.document_id "
                        f"WHERE q.created_at >= {since} {after}"
                        "GROUP BY q.id, q.created_at, u.email, q.question, q.model_used, "
                        "  q.latency_retrieval_ms, q.latency_generation_ms "
                        # One more than asked for: whether another page exists is knowable
                        # from the overshoot, and a count(*) to answer it would scan the
                        # whole window on every page turn.
                        f"ORDER BY q.created_at DESC, q.id DESC LIMIT {size + 1}"
                    ),
                    parameters,
                )
            )

        entries = [
            AuditEntry(
                query_id=row.id,
                asked_at=row.created_at,
                email=row.email,
                question=row.question,
                abstained=len(row.documents) == 0,
                model=row.model_used,
                latency_ms=int(row.latency_ms),
                documents=sorted(row.documents),
            )
            for row in rows[:size]
        ]
        return AuditPage(
            entries=entries,
            next_cursor=(
                Cursor(created_at=entries[-1].asked_at, id=entries[-1].query_id).encode()
                if len(rows) > size and entries
                else None
            ),
        )
