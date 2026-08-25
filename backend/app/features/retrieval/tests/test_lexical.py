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
from app.features.retrieval.lexical import CONFIGURATION, to_tsquery

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


#: Spanish as it is actually typed: in a hurry, on a keyboard that makes accents awkward, or
#: on a phone. Every one of these returned nothing from the lexical half before 0018.
ACCENTED = (
    ("máximo", "maximo"),
    ("detención", "detencion"),
    ("artículo", "articulo"),
    ("código", "codigo"),
    ("garantía", "garantia"),
)


@pytest.mark.parametrize(("stored", "typed"), ACCENTED)
async def test_a_word_typed_without_its_accent_still_matches(
    configured_engines: None, stored: str, typed: str
) -> None:
    """Half a search, quietly, on exactly the corpus this product is sold into.

    `to_tsvector('english', 'máximo')` and `to_tsquery('english', 'maximo')` do not match:
    the English configuration folds no accents. Nothing errored — the dense half still
    answered — so the only symptom was worse results for anybody typing the way most people
    type Spanish.
    """
    async with unscoped_session() as session:
        matched = await session.scalar(
            text("SELECT to_tsvector(:config, :stored) @@ to_tsquery(:config, :query)"),
            {"config": CONFIGURATION, "stored": stored, "query": await to_tsquery(session, typed)},
        )

    assert matched is True


@pytest.mark.parametrize(("stored", "typed"), ACCENTED)
async def test_it_matches_in_the_other_direction_too(
    configured_engines: None, stored: str, typed: str
) -> None:
    """Folding is on both sides, so the accented spelling finds the unaccented text as well.

    Worth asserting separately: a configuration applied to the index alone would pass the test
    above by accident, and would be the exact asymmetry `lexical.py` was written to forbid.
    """
    async with unscoped_session() as session:
        matched = await session.scalar(
            text("SELECT to_tsvector(:config, :stored) @@ to_tsquery(:config, :query)"),
            {"config": CONFIGURATION, "stored": typed, "query": await to_tsquery(session, stored)},
        )

    assert matched is True


async def test_english_stemming_is_untouched(configured_engines: None) -> None:
    """The whole point of folding accents in front of the English stemmer rather than
    switching configuration.

    Seventy-seven per cent of this corpus is English. `'spanish'` would have fixed the accents
    and taken English stemming and stop-words away from three quarters of the passages to do
    it, which is why 0018 does neither.
    """
    async with unscoped_session() as session:
        stemmed = await session.scalar(
            text("SELECT to_tsvector(:config, 'running quickly') @@ to_tsquery(:config, :query)"),
            {"config": CONFIGURATION, "query": await to_tsquery(session, "run")},
        )
        stopword = await session.scalar(
            text("SELECT to_tsvector(:config, 'the report')::text"),
            {"config": CONFIGURATION},
        )

    assert stemmed is True, "English stemming must still conflate run/running"
    assert "the" not in stopword, "English stop-words must still be removed"


@pytest.mark.parametrize("identifier", IDENTIFIERS)
async def test_folding_leaves_identifiers_whole(configured_engines: None, identifier: str) -> None:
    """`unaccent` is mapped only over token types that can carry letters.

    Numbers and the punctuation inside `1545-0074` are left to the parser that already stores
    them whole — the property the identifier search depends on, and the one a regex broke
    once already.
    """
    async with unscoped_session() as session:
        matched = await session.scalar(
            text("SELECT to_tsvector(:config, :stored) @@ to_tsquery(:config, :query)"),
            {
                "config": CONFIGURATION,
                "stored": f"see {identifier} for details",
                "query": await to_tsquery(session, identifier),
            },
        )

    assert matched is True
