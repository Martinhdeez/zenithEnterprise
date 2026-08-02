"""The tokeniser bug that nearly cost us ParadeDB, as a regression test.

M0 tokenised queries with `re.findall(r"[A-Za-z0-9']+", ...)` while the corpus side was
`to_tsvector('english', text)`. Identifiers survived indexing and were destroyed at query
time, so lexical search found one identifier in six and looked worthless. Fixed, it found
four — and two of those are lexical-only, which is the evidence the hybrid design rests on.

These tests run the real analyser, because the entire failure was an assumption about what
the analyser does.
"""

import pytest
from sqlalchemy import text

from app.core.database import unscoped_session
from app.features.retrieval.lexical import to_tsquery

pytestmark = pytest.mark.asyncio

IDENTIFIERS = ("119/33", "1545-0074", "ISO/IEC 27001", "EUR-Lex 32016R0679")


@pytest.mark.parametrize("identifier", IDENTIFIERS)
async def test_an_identifier_survives_the_round_trip(
    configured_engines: None, identifier: str
) -> None:
    """The property the regex broke.

    `119/33` became `119`; `1545-0074` became `1545` and `0074`. Both were stored whole, so
    the query could never match what the index held — and nothing errored, the results were
    merely worse.
    """
    async with unscoped_session() as session:
        query = await to_tsquery(session, f"What does {identifier} say?")
        matched = await session.scalar(
            text("SELECT to_tsvector('english', :body) @@ to_tsquery('english', :query)"),
            {"body": f"Regulation {identifier} sets out the requirements.", "query": query},
        )

    assert matched is True, f"{identifier!r} does not match itself after tokenisation"


async def test_the_query_uses_the_same_analyser_as_the_index(configured_engines: None) -> None:
    """Stated directly, because "looks equivalent" is exactly what went wrong.

    Anything that tokenises a query with its own rules will disagree with the generated
    `tsv` column somewhere, and the disagreement is silent: no error, just worse results
    that get attributed to the ranking model.
    """
    async with unscoped_session() as session:
        query = await to_tsquery(session, "controller obligations")
        lexemes = await session.scalar(
            text("SELECT tsvector_to_array(to_tsvector('english', :body))"),
            {"body": "controller obligations"},
        )

    assert set(query.split(" | ")) == set(lexemes or [])


async def test_terms_are_ord_not_anded(configured_engines: None) -> None:
    """A question is not a filter.

    Requiring every lexeme returns nothing for anything conversational; ranking already
    puts passages containing more of them first, and fusion with the dense half covers the
    remainder.
    """
    async with unscoped_session() as session:
        query = await to_tsquery(session, "controller processor obligations")
        partial = await session.scalar(
            text("SELECT to_tsvector('english', :body) @@ to_tsquery('english', :query)"),
            {"body": "The controller shall document its decisions.", "query": query},
        )

    assert " | " in query
    assert partial is True


async def test_a_question_with_no_lexemes_is_empty_rather_than_invalid(
    configured_engines: None,
) -> None:
    """`to_tsquery` raises on an empty string, so the caller has to be able to see that
    there is nothing to search for rather than discovering it as a database error."""
    async with unscoped_session() as session:
        assert await to_tsquery(session, "?? !!") == ""
