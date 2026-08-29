"""Reading the query log back, and the isolation RLS cannot provide here.

The assertion that matters is not that history works. It is that a member holding only
`query.history.own` cannot read their colleagues' questions — because `queries` is
tenant-scoped by policy, so nothing in the database stops that. The filter is application
code, which this project normally refuses, and it is therefore tested like the security
control it is.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.common.exceptions import PermissionDeniedError
from app.core.database import owner_session
from app.features.auth.service import AccessProfile
from app.features.query.history import ANY, OWN, HistoryService
from app.features.tenancy.context import TenantContext
from conftest import Account


async def log(tenant_id: UUID, user_id: UUID, question: str, citations: int = 0) -> UUID:
    """Write a query row the way `AnswerService` does, through the owner connection.

    Seeded through the owner deliberately: a fixture built with the code under test can
    only prove that code agrees with itself.
    """
    async with owner_session() as session:
        query_id = await session.scalar(
            text(
                "INSERT INTO queries (tenant_id, user_id, question, answer, model_used, "
                "latency_retrieval_ms, latency_generation_ms) "
                "VALUES (:t, :u, :q, 'an answer', 'stub-8b', 1200, 9000) RETURNING id"
            ),
            {"t": tenant_id, "u": user_id, "q": question},
        )
        for _ in range(citations):
            chunk_id = await session.scalar(
                text(
                    "INSERT INTO chunks (document_id, tenant_id, page_num, char_start, "
                    "char_end, text) SELECT d.id, :t, 1, 0, 5, 'text' FROM documents d "
                    "WHERE d.tenant_id = :t LIMIT 1 RETURNING id"
                ),
                {"t": tenant_id},
            )
            if chunk_id:
                await session.execute(
                    text(
                        "INSERT INTO query_citations (query_id, tenant_id, chunk_id, rank) "
                        "SELECT :q, q.tenant_id, :c, 1 FROM queries q WHERE q.id = :q"
                    ),
                    {"q": query_id, "c": chunk_id},
                )
    return UUID(str(query_id))


def profile_with(account: Account, user_id: UUID, permissions: set[str]) -> AccessProfile:
    return AccessProfile(
        user_id=user_id,
        context=TenantContext.for_tenant(account.tenant_id, [account.default_label]),
        permissions=frozenset(permissions),
    )


async def test_a_member_reads_their_own_history(account: Account) -> None:
    await log(account.tenant_id, account.member_id, "what is my notice period?")

    page = await HistoryService(profile_with(account, account.member_id, {OWN})).page()

    assert [entry.question for entry in page.entries] == ["what is my notice period?"]
    assert page.entries[0].mine is True


async def test_a_member_cannot_read_a_colleagues_history(account: Account) -> None:
    """The assertion this module exists for.

    `queries` is tenant-scoped by RLS, so the database would happily return this row. The
    questions people ask are more revealing than the documents they read — "what is my
    severance?", "can I be dismissed for this?" — and a history endpoint that leaked them
    would look like a working feature.
    """
    await log(account.tenant_id, account.admin_id, "what is the redundancy budget?")

    page = await HistoryService(profile_with(account, account.member_id, {OWN})).page()

    assert page.entries == []


async def test_an_admin_reads_the_whole_tenant(account: Account) -> None:
    """`query.history.any` exists for the audit trail iteration 4 needs, and it is a
    separate permission precisely because it is a different thing to grant."""
    await log(account.tenant_id, account.member_id, "member question")
    await log(account.tenant_id, account.admin_id, "admin question")

    page = await HistoryService(profile_with(account, account.admin_id, {ANY})).page()

    assert {entry.question for entry in page.entries} == {"member question", "admin question"}


async def test_the_reader_can_tell_whose_question_it_was(account: Account) -> None:
    """A shared history is only readable if you can tell them apart."""
    await log(account.tenant_id, account.member_id, "theirs")
    await log(account.tenant_id, account.admin_id, "mine")

    page = await HistoryService(profile_with(account, account.admin_id, {ANY})).page()
    by_question = {entry.question: entry.mine for entry in page.entries}

    assert by_question == {"mine": True, "theirs": False}


async def test_another_tenant_sees_nothing(account: Account) -> None:
    """Tenant isolation is still the database's job, and still holds."""
    await log(account.tenant_id, account.member_id, "a question")
    intruder = AccessProfile(
        user_id=uuid4(),
        context=TenantContext.for_tenant(uuid4()),
        permissions=frozenset({ANY}),
    )

    assert (await HistoryService(intruder).page()).entries == []


async def test_holding_neither_permission_is_refused(account: Account) -> None:
    """A 403 rather than an empty page: the caller is asking for something they may not
    have, which is different from having nothing."""
    with pytest.raises(PermissionDeniedError):
        await HistoryService(profile_with(account, account.member_id, {"query.execute"})).page()


async def test_pages_do_not_repeat_or_skip_rows(account: Account) -> None:
    """Keyset, for the reason F4 chose it for documents: the query log is written by every
    query, so something is always inserted between two pages, and an offset would repeat or
    skip a row every time."""
    for index in range(5):
        await log(account.tenant_id, account.member_id, f"question {index}")
    service = HistoryService(profile_with(account, account.member_id, {OWN}))

    first = await service.page(limit=2)
    second = await service.page(limit=2, cursor=first.next_cursor)

    assert len(first.entries) == 2
    assert first.next_cursor is not None
    seen = [entry.query_id for entry in [*first.entries, *second.entries]]
    assert len(set(seen)) == len(seen), "no row may appear on two pages"


async def test_the_last_page_reports_no_cursor(account: Account) -> None:
    await log(account.tenant_id, account.member_id, "only one")

    page = await HistoryService(profile_with(account, account.member_id, {OWN})).page(limit=10)

    assert page.next_cursor is None


async def test_the_policy_hides_a_colleagues_history_even_without_the_service(
    account: Account,
) -> None:
    """The assertion that proves the enforcement moved into the database.

    F15 filtered by `user_id` in Python and documented it as the one place application code
    did security work. Migration 0005 replaced that with a policy — so a raw `SELECT *`,
    written by somebody who never read `history.py`, must now return nothing. That is the
    whole difference between a rule and a convention.
    """
    from app.core.database import tenant_session

    await log(account.tenant_id, account.admin_id, "what is the redundancy budget?")
    theirs = TenantContext.for_tenant(
        account.tenant_id,
        [account.default_label],
        user_id=account.member_id,
        reads_all_history=False,
    )

    async with tenant_session(theirs) as session:
        rows = await session.execute(text("SELECT id, question FROM queries"))
        assert rows.all() == []


async def test_an_unbound_user_reads_nothing_rather_than_everything(
    account: Account,
) -> None:
    """Failing closed, in the same direction as every other policy here.

    `zenith.user_id` unset is NULL, and `user_id = NULL` is never true. A session that
    forgets to bind the caller — a background job, a future code path — sees no history at
    all, which is the safe half of the mistake.
    """
    from app.core.database import tenant_session

    await log(account.tenant_id, account.member_id, "a question")
    unbound = TenantContext.for_tenant(account.tenant_id, [account.default_label])

    async with tenant_session(unbound) as session:
        assert (await session.execute(text("SELECT id FROM queries"))).all() == []


async def test_citations_of_an_unreadable_query_are_unreadable_too(
    account: Account,
) -> None:
    """`query_citations` needed no change: its policy is `EXISTS (SELECT 1 FROM queries…)`,
    which applies the parent's policy in turn. That is why derived policies were written
    that way in migration 0001, and this is the first time it has paid off."""
    from app.core.database import tenant_session

    query_id = await log(account.tenant_id, account.admin_id, "private", citations=1)
    theirs = TenantContext.for_tenant(
        account.tenant_id, [account.default_label], user_id=account.member_id
    )

    async with tenant_session(theirs) as session:
        rows = await session.execute(
            text("SELECT chunk_id FROM query_citations WHERE query_id = :q"), {"q": query_id}
        )
        assert rows.all() == []


# --- searching and filtering ------------------------------------------------------------


async def test_search_matches_a_fragment_of_the_question(account: Account) -> None:
    """A substring, not a stemmed term. Somebody looking for the question they asked on
    Tuesday types a piece of it, and `sever` should find "what is my severance?"."""
    await log(account.tenant_id, account.admin_id, "what is my severance?")
    await log(account.tenant_id, account.admin_id, "how do I book leave?")
    reader = HistoryService(profile_with(account, account.admin_id, {ANY}))

    page = await reader.page(search="sever")

    assert [entry.question for entry in page.entries] == ["what is my severance?"]


async def test_search_ignores_the_answer(account: Account) -> None:
    """Matching the answer would surface somebody's question because of words the *model*
    wrote — a confusing result, and on a shared history a slightly invasive one. Every row
    `log` writes has the answer 'an answer', so a search for it must find nothing."""
    await log(account.tenant_id, account.admin_id, "a question about leave")
    reader = HistoryService(profile_with(account, account.admin_id, {ANY}))

    page = await reader.page(search="an answer")

    assert page.entries == []


async def test_a_percent_sign_is_searched_for_rather_than_matched_as_a_wildcard(
    account: Account,
) -> None:
    """`%` and `_` are ordinary characters in a question and wildcards in `ILIKE`.
    Unescaped, a search for "50%" would match every row."""
    await log(account.tenant_id, account.admin_id, "is the rate 50% or 20%?")
    await log(account.tenant_id, account.admin_id, "something else entirely")
    reader = HistoryService(profile_with(account, account.admin_id, {ANY}))

    page = await reader.page(search="50%")

    assert len(page.entries) == 1


async def test_mine_narrows_a_shared_history_to_the_caller(account: Account) -> None:
    await log(account.tenant_id, account.admin_id, "mine")
    await log(account.tenant_id, account.member_id, "a colleague's")
    reader = HistoryService(profile_with(account, account.admin_id, {ANY}))

    everyones = await reader.page()
    just_mine = await reader.page(mine_only=True)

    assert len(everyones.entries) == 2
    assert [entry.question for entry in just_mine.entries] == ["mine"]


async def test_the_filters_cannot_widen_what_the_policy_narrowed(account: Account) -> None:
    """`mine=false` is the default and is not a request to see other people. Whether this
    caller reads anybody else's questions is decided from their permissions and handed to
    migration 0005's policy — there is no parameter for it, deliberately."""
    await log(account.tenant_id, account.admin_id, "mine")
    await log(account.tenant_id, account.member_id, "a colleague's")
    reader = HistoryService(profile_with(account, account.admin_id, {OWN}))

    page = await reader.page(mine_only=False)

    assert [entry.question for entry in page.entries] == ["mine"]


async def test_unanswered_finds_the_questions_the_corpus_could_not_answer(
    account: Account,
    labelled_document: UUID,  # `log(citations=...)` needs a document to cite
) -> None:
    """What is missing from the corpus, which is the most useful thing this screen can
    surface. Derived from the citation rows the same way the analytics abstention count is,
    so the two cannot disagree."""
    await log(account.tenant_id, account.admin_id, "answered", citations=1)
    await log(account.tenant_id, account.admin_id, "found nothing")
    reader = HistoryService(profile_with(account, account.admin_id, {ANY}))

    page = await reader.page(unanswered_only=True)

    assert [entry.question for entry in page.entries] == ["found nothing"]


async def test_filters_combine(account: Account) -> None:
    await log(account.tenant_id, account.admin_id, "my unanswered question")
    await log(account.tenant_id, account.member_id, "their unanswered question")
    reader = HistoryService(profile_with(account, account.admin_id, {ANY}))

    page = await reader.page(mine_only=True, unanswered_only=True)

    assert [entry.question for entry in page.entries] == ["my unanswered question"]


async def test_a_cursor_still_works_across_a_filtered_page(account: Account) -> None:
    """No sort is encoded in the cursor, unlike `LabelCursor`, and none needs to be: the
    ordering is always newest-first, so a cursor means "older than this row" whatever the
    filters remove."""
    for index in range(4):
        await log(account.tenant_id, account.admin_id, f"question {index}")
    reader = HistoryService(profile_with(account, account.admin_id, {ANY}))

    first = await reader.page(limit=2, search="question")
    second = await reader.page(limit=2, cursor=first.next_cursor, search="question")

    seen = [entry.question for entry in first.entries + second.entries]

    assert len(set(seen)) == 4
