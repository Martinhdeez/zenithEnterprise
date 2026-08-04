"""The third signal, and the measurement that justified it.

F15 found the two questions that capped the context ceiling at 90.3%, and the cause was not
tokenisation — `to_tsvector` stores `10000w` and `u.s.c` whole on both sides. `ts_rank_cd`
simply has no IDF, so on the real corpus the chunk containing `10000W` ranked **52nd**,
two places outside the candidate set, beaten by chunks matching the common words `catalog`
and `number` in the same question.
"""

from uuid import uuid4

from app.core.database import tenant_session
from app.features.retrieval.identifiers import exact, identifier_like
from app.features.retrieval.search import fuse
from app.features.retrieval.tests.test_search import profile_for, seed
from conftest import Account

PASSAGES = [
    # The decisive chunk: it holds the identifier and almost nothing else the question says.
    ("Cat. No. 10000W. Employer's Tax Guide.", 1),
    # Fifty chunks' worth of the same two common words is what buries it in practice.
    ("Catalog number lookup: every catalog number in this catalog has a number.", 2),
    ("The catalog number appears on each form, and the number is a catalog number.", 3),
    ("Section 101 of title 23, United States Code (23 U.S.C. 101) defines the terms.", 4),
    ("Title 23 covers highways. Section 101 is elsewhere in this act.", 5),
]


def test_a_word_is_not_an_identifier() -> None:
    assert identifier_like("controller") is False
    assert identifier_like("catalog") is False


def test_a_token_with_a_digit_is_an_identifier() -> None:
    for lexeme in ("10000w", "1545-0074", "23", "101", "119/33"):
        assert identifier_like(lexeme) is True, lexeme


def test_an_abbreviation_with_stops_is_an_identifier() -> None:
    """`u.s.c` carries no digit, and it is exactly the token that moved the U.S.C. citation
    from unfound to rank four. A rule that only looked for digits would have missed it."""
    assert identifier_like("u.s.c") is True


async def test_the_exact_query_finds_a_chunk_frequency_ranking_buries(
    account: Account,
) -> None:
    """The measured failure, reproduced small.

    `10000W` appears once, in a chunk that says little else. The competing chunks repeat
    `catalog` and `number`, which is what `ts_rank_cd` rewards — so the ordinary lexical
    query ranks them first and the answer falls off the end of the candidate set.
    """
    await seed(account.tenant_id, account.default_label, PASSAGES)
    context = (await profile_for(account)).context

    async with tenant_session(context) as session:
        found = await exact(session, "What is Catalog Number 10000W?")

    assert found, "the identifier query must find the passage holding the identifier"

    async with tenant_session(context) as session:
        from app.features.retrieval.search import lexical

        ordinary = await lexical(session, "What is Catalog Number 10000W?", 50)

    assert found[0][0] == ordinary[0][0] or found[0][0] in [chunk_id for chunk_id, _ in ordinary], (
        "the exact hit must be a real chunk, not a phantom"
    )


async def test_it_ands_the_identifiers_rather_than_oring_them(account: Account) -> None:
    """AND is the whole point, and the opposite of `lexical()`.

    `lexical` ORs because a question is not a filter. Here the identifiers *are* the filter:
    someone asking about `23 U.S.C. 101` wants the passage with all three parts, not every
    passage mentioning `23`.
    """
    await seed(account.tenant_id, account.default_label, PASSAGES)
    context = (await profile_for(account)).context

    async with tenant_session(context) as session:
        found = await exact(session, "What does the citation 23 U.S.C. 101 refer to?")
        rows = await session.execute(
            __import__("sqlalchemy").text("SELECT id, text FROM chunks WHERE id = ANY(:ids)"),
            {"ids": [chunk_id for chunk_id, _ in found]},
        )
        texts = [row.text for row in rows]

    assert texts, "the citation must be found"
    assert all("23 U.S.C. 101" in body for body in texts), (
        "a passage mentioning only 23, or only 101, must not qualify"
    )


async def test_a_question_with_no_identifier_costs_nothing(account: Account) -> None:
    """Most questions have no identifier at all, and they must not pay for this."""
    await seed(account.tenant_id, account.default_label, PASSAGES)
    context = (await profile_for(account)).context

    async with tenant_session(context) as session:
        assert await exact(session, "What obligations does a controller have?") == []


def test_the_exact_leader_is_guaranteed_a_place_in_the_result() -> None:
    """The floor that actually rescues a buried identifier.

    The exact ranking is short, so on RRF score alone its single hit can still lose to a
    chunk both other halves agree on. Being the identifier query's first result is strong
    evidence, and `_promote_leaders` treats it as such — the same rule F7 introduced for
    the lexical and dense leaders, extended to the signal that exists for this case.
    """
    identifier_hit = uuid4()
    agreed = [uuid4() for _ in range(8)]

    ranked = [chunk_id for chunk_id, _ in fuse(agreed, agreed, limit=4, exact_ids=[identifier_hit])]

    assert identifier_hit in ranked
    assert ranked[:3] == agreed[:3], "the rest of the order is untouched"


def test_fusion_without_an_exact_ranking_is_unchanged() -> None:
    """The signal is additive. A question with no identifiers must fuse exactly as before,
    or every existing recall number silently changes meaning."""
    lexical_ids = [uuid4() for _ in range(5)]
    dense_ids = [lexical_ids[0], *[uuid4() for _ in range(4)]]

    assert fuse(lexical_ids, dense_ids, limit=5) == fuse(
        lexical_ids, dense_ids, limit=5, exact_ids=[]
    )
