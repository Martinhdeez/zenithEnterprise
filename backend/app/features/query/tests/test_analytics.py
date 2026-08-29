"""The dashboard's numbers, and the policy that decides whose activity they describe.

Almost nothing here is new data — `queries` and `query_citations` have logged it since 0001
and nothing had ever read them. So most of these tests are about the two places an aggregate
quietly lies: counting a citation as an answer, and counting an unknown cost as zero.

The scoping test is the one that matters. Analytics are read through the same policy the
history screen uses, so an administrator without `query.history.any` sees their own activity
rather than the tenant's — enforced in the database rather than by a filter somebody has to
remember to write.
"""

from uuid import UUID, uuid4

from sqlalchemy import text

from app.core.database import owner_session
from app.features.auth.access.permissions import CATALOGUE
from app.features.auth.service import AccessProfile
from app.features.documents.pagination import Cursor
from app.features.query.analytics import AnalyticsService
from app.features.query.history import ANY, OWN
from app.features.tenancy.context import TenantContext
from conftest import Account


def profile(account: Account, user_id: UUID, *, everyones: bool) -> AccessProfile:
    return AccessProfile(
        user_id=user_id,
        context=TenantContext.for_tenant(account.tenant_id, [account.default_label]),
        permissions=frozenset(CATALOGUE) if everyones else frozenset({OWN}),
    )


async def asked(
    account: Account,
    user_id: UUID,
    question: str = "what is this?",
    *,
    prompt_tokens: int | None = 100,
    completion_tokens: int | None = 50,
    cites: UUID | None = None,
) -> UUID:
    """One row in the query log, written through the owner connection.

    Seeded past the code under test on purpose: a fixture built with the thing it is
    checking can only prove that thing agrees with itself.
    """
    async with owner_session() as session:
        query_id = await session.scalar(
            text(
                "INSERT INTO queries (tenant_id, user_id, question, answer, model_used, "
                "latency_retrieval_ms, latency_generation_ms, prompt_tokens, "
                "completion_tokens) VALUES (:t, :u, :q, 'an answer', 'test-model', 100, 900, "
                ":pt, :ct) RETURNING id"
            ),
            {
                "t": account.tenant_id,
                "u": user_id,
                "q": question,
                "pt": prompt_tokens,
                "ct": completion_tokens,
            },
        )
        if cites:
            await session.execute(
                text(
                    "INSERT INTO query_citations (query_id, tenant_id, chunk_id, rank) "
                    "SELECT :q, q.tenant_id, :c, 1 FROM queries q WHERE q.id = :q"
                ),
                {"q": query_id, "c": cites},
            )
        return UUID(str(query_id))


async def a_chunk(account: Account, filename: str = "report.pdf") -> tuple[UUID, UUID]:
    async with owner_session() as session:
        document_id = await session.scalar(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes, status) "
                "VALUES (:t, :f, :sha, 10, 'ready') RETURNING id"
            ),
            {"t": account.tenant_id, "f": filename, "sha": uuid4().hex + uuid4().hex[:32]},
        )
        chunk_id = await session.scalar(
            text(
                "INSERT INTO chunks (document_id, tenant_id, page_num, char_start, char_end, "
                "text) VALUES (:d, :t, 1, 0, 10, 'passage') RETURNING id"
            ),
            {"d": document_id, "t": account.tenant_id},
        )
        return UUID(str(document_id)), UUID(str(chunk_id))


# --- scoping --------------------------------------------------------------------------


async def test_without_reading_everyones_history_the_numbers_are_your_own(
    account: Account,
) -> None:
    """The property that stops this being a way around the history policy.

    Not enforced here — migration 0005's policy narrows `queries` to the caller when
    `zenith.reads_all_history` is false, so this holds for any statement in this module,
    including ones written later by somebody who never read this test.
    """
    await asked(account, account.admin_id)
    await asked(account, account.member_id)

    mine = await AnalyticsService(profile(account, account.member_id, everyones=False)).overview()

    assert mine.totals.queries == 1


async def test_reading_everyones_history_shows_the_tenant(account: Account) -> None:
    await asked(account, account.admin_id)
    await asked(account, account.member_id)

    everyone = await AnalyticsService(profile(account, account.admin_id, everyones=True)).overview()

    assert everyone.totals.queries == 2
    assert everyone.totals.users == 2


async def test_another_tenants_activity_is_absent(account: Account) -> None:
    """RLS, not a `WHERE tenant_id` in the analytics SQL."""
    from app.features.tenancy.service import TenantService

    other = await TenantService().create(f"Other {uuid4()}")
    async with owner_session() as session:
        outsider = await session.scalar(
            text(
                "INSERT INTO users (tenant_id, email, password_hash) "
                "VALUES (:t, :e, 'x') RETURNING id"
            ),
            {"t": other.id, "e": f"outsider-{uuid4()}@example.com"},
        )
        await session.execute(
            text(
                "INSERT INTO queries (tenant_id, user_id, question) "
                "VALUES (:t, :u, 'their question')"
            ),
            {"t": other.id, "u": outsider},
        )

    reader = AnalyticsService(profile(account, account.admin_id, everyones=True))

    assert (await reader.overview()).totals.queries == 0
    assert all("their question" not in e.question for e in (await reader.audit()).entries)


# --- the aggregates that would quietly lie ----------------------------------------------


async def test_an_unreported_cost_is_not_a_zero_cost(account: Account) -> None:
    """The distinction `GenerationResponse` exists to preserve, carried to the screen.

    A local binding reports no usage and a gateway may strip it. Summing NULLs as zeros
    would produce a confident, wrong number in a cost report somebody budgets against.
    """
    await asked(account, account.admin_id, prompt_tokens=100, completion_tokens=50)
    await asked(account, account.admin_id, prompt_tokens=None, completion_tokens=None)

    totals = (
        await AnalyticsService(profile(account, account.admin_id, everyones=True)).overview()
    ).totals

    assert totals.prompt_tokens == 100
    assert totals.queries_without_usage == 1
    assert totals.queries == 2


async def test_a_document_cited_twice_in_one_answer_counts_once(account: Account) -> None:
    """`count(DISTINCT query_id)`, not `count(*)`. Counting citations would rank long
    documents above useful ones — a fifty-page report quoted four times in one answer would
    outrank a one-page policy that answered four separate questions."""
    document_id, chunk_id = await a_chunk(account)
    query_id = await asked(account, account.admin_id, cites=chunk_id)

    async with owner_session() as session:
        second = await session.scalar(
            text(
                "INSERT INTO chunks (document_id, tenant_id, page_num, char_start, char_end, "
                "text) VALUES (:d, :t, 2, 0, 10, 'another passage') RETURNING id"
            ),
            {"d": document_id, "t": account.tenant_id},
        )
        await session.execute(
            text(
                "INSERT INTO query_citations (query_id, tenant_id, chunk_id, rank) "
                "SELECT :q, q.tenant_id, :c, 2 FROM queries q WHERE q.id = :q"
            ),
            {"q": query_id, "c": second},
        )

    top = (
        await AnalyticsService(profile(account, account.admin_id, everyones=True)).overview()
    ).top_cited

    assert top[0].filename == "report.pdf"
    assert top[0].answers == 1


async def test_an_answer_that_cited_nothing_is_an_abstention(account: Account) -> None:
    """Derived from the citation rows rather than stored: `queries` has no `abstained`
    column, and the citations are the same evidence the answer was bound against."""
    _, chunk_id = await a_chunk(account)
    await asked(account, account.admin_id, cites=chunk_id)
    await asked(account, account.admin_id, question="nothing matches this")

    totals = (
        await AnalyticsService(profile(account, account.admin_id, everyones=True)).overview()
    ).totals

    assert totals.abstentions == 1


async def test_the_audit_row_names_the_documents_the_answer_read(account: Account) -> None:
    """The point of the table. What an answer *read* is what an auditor asks about, and it
    is not derivable from the answer text."""
    _, chunk_id = await a_chunk(account, filename="payroll.pdf")
    await asked(account, account.admin_id, question="who was paid what?", cites=chunk_id)

    page = await AnalyticsService(profile(account, account.admin_id, everyones=True)).audit()

    assert page.entries[0].question == "who was paid what?"
    assert page.entries[0].documents == ["payroll.pdf"]
    assert not page.entries[0].abstained


async def test_the_most_active_list_is_ordered_by_activity(account: Account) -> None:
    await asked(account, account.member_id)
    for _ in range(3):
        await asked(account, account.admin_id)

    active = (
        await AnalyticsService(profile(account, account.admin_id, everyones=True)).overview()
    ).most_active

    assert active[0].queries == 3
    assert active[0].email == account.admin_email


async def test_an_empty_installation_reports_zeroes_rather_than_failing(
    account: Account,
) -> None:
    """A brand-new tenant opens this screen before asking anything, and `avg()` over no rows
    is NULL — which is an exception on the way to an int, not a zero."""
    overview = await AnalyticsService(profile(account, account.admin_id, everyones=True)).overview()

    reader = AnalyticsService(profile(account, account.admin_id, everyones=True))
    page = await reader.audit()

    assert overview.totals.queries == 0
    assert overview.totals.average_generation_ms == 0
    assert page.entries == []
    assert page.next_cursor is None


async def test_the_permissions_needed_to_read_it_exist(account: Account) -> None:
    """The route is gated on these two codes; a permission nobody enforces is a lie in the
    administration screen."""
    assert {OWN, ANY} <= set(CATALOGUE)


# --- the audit log's pages --------------------------------------------------------------


async def test_a_page_stops_at_the_limit_and_offers_a_cursor(account: Account) -> None:
    for index in range(4):
        await asked(account, account.admin_id, question=f"question {index}")

    page = await AnalyticsService(profile(account, account.admin_id, everyones=True)).audit(limit=2)

    assert len(page.entries) == 2
    assert page.next_cursor is not None


async def test_the_last_page_offers_no_cursor(account: Account) -> None:
    """Known from asking for one row more than the page needs. A `count(*)` to answer the
    same question would scan the whole window on every page turn."""
    await asked(account, account.admin_id)
    await asked(account, account.admin_id)

    page = await AnalyticsService(profile(account, account.admin_id, everyones=True)).audit(limit=5)

    assert len(page.entries) == 2
    assert page.next_cursor is None


async def test_the_next_page_resumes_without_repeating_or_skipping(account: Account) -> None:
    """The property `OFFSET` cannot give: a question asked while somebody is reading shifts
    an offset by one, so the reader sees a row twice or never sees it at all."""
    for index in range(6):
        await asked(account, account.admin_id, question=f"question {index}")

    reader = AnalyticsService(profile(account, account.admin_id, everyones=True))
    first = await reader.audit(limit=3)
    second = await reader.audit(cursor=Cursor.decode(first.next_cursor or ""), limit=3)

    seen = [entry.question for entry in first.entries + second.entries]

    assert len(seen) == 6
    assert len(set(seen)) == 6, "no question appears on two pages"


async def test_questions_asked_in_the_same_instant_are_not_lost(account: Account) -> None:
    """Why `id` is in the key and not just the timestamp.

    Two rows sharing a `created_at` would make a timestamp-only cursor skip one of them or
    return it forever — the failure `LabelCursor` hit when migration 0007 backfilled every
    label with one timestamp.
    """
    from datetime import UTC, datetime

    stamp = datetime.now(UTC)
    async with owner_session() as session:
        for index in range(4):
            await session.execute(
                text(
                    "INSERT INTO queries (tenant_id, user_id, question, created_at) "
                    "VALUES (:t, :u, :q, :at)"
                ),
                {
                    "t": account.tenant_id,
                    "u": account.admin_id,
                    "q": f"simultaneous {index}",
                    "at": stamp,
                },
            )

    reader = AnalyticsService(profile(account, account.admin_id, everyones=True))
    first = await reader.audit(limit=2)
    second = await reader.audit(cursor=Cursor.decode(first.next_cursor or ""), limit=2)

    seen = [entry.question for entry in first.entries + second.entries]

    assert sorted(seen) == [f"simultaneous {i}" for i in range(4)]


async def test_a_cursor_we_did_not_issue_is_refused(account: Account) -> None:
    """400 rather than a silent restart from the top: a malformed cursor means the client is
    confused about where it is, and results that look correct would hide that."""
    import pytest

    from app.common.exceptions import InvalidCursorError

    with pytest.raises(InvalidCursorError):
        Cursor.decode("not-a-cursor-we-made")


async def test_the_page_is_newest_first(account: Account) -> None:
    await asked(account, account.admin_id, question="older")
    await asked(account, account.admin_id, question="newer")

    page = await AnalyticsService(profile(account, account.admin_id, everyones=True)).audit()

    assert page.entries[0].question == "newer"
