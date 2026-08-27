"""Routing, tested against M0's three real extraction failures.

Each case here is a document that actually broke, reconstructed as the smallest page that
reproduces the signature.
"""

import pytest

from app.features.ingestion import routing
from app.features.ingestion.parsers.base import Box, ParsedPage, Word
from app.features.ingestion.routing import Route, decide

PROSE = (
    "The Regulation applies to the processing of personal data wholly or partly by "
    "automated means. It also applies to processing other than by automated means of "
    "personal data which form part of a filing system. " * 3
)


def page(text: str, words: tuple[Word, ...] = ()) -> ParsedPage:
    return ParsedPage(page_num=1, text=text, words=words)


# Both fixtures are above `MIN_WORDS_FOR_LAYOUT`, which is 250: a sparse page is not judged
# at all, because empty bins on such a page come from sparsity rather than from layout.
#
# The word centres are spread continuously, the way they fall on a real page. An earlier
# version placed them at five fixed positions per column, which made most bins empty and the
# median density zero — the fixture failed to look like a page at all, and the detector was
# right to decline to judge it.
WORDS = 400


def columns() -> tuple[Word, ...]:
    """Two blocks of text with a gutter between them — the IRS and BERT signature."""
    words: list[Word] = []
    for index in range(WORDS):
        side, position = divmod(index, 40)
        top = round((index % 45) * 0.018, 4)
        left = (0.08 if side % 2 else 0.54) + position * 0.0095
        words.append(
            Word(f"w{index}", Box(1, round(left, 4), top, round(left + 0.008, 4), top + 0.01))
        )
    return tuple(words)


def single_column() -> tuple[Word, ...]:
    """The same amount of text spread evenly across the full width."""
    words: list[Word] = []
    for index in range(WORDS):
        position = index % 88
        top = round((index % 45) * 0.018, 4)
        left = 0.06 + position * 0.0095
        words.append(
            Word(f"w{index}", Box(1, round(left, 4), top, round(left + 0.008, 4), top + 0.01))
        )
    return tuple(words)


def test_a_page_with_no_text_layer_is_unreadable_even_where_ocr_is_allowed() -> None:
    """The NASA case: a scan whose publisher never ran OCR. 925 characters per page against
    the ~4,000 an equivalent text page carries.

    The profile permitting OCR is not the same as this build being able to do it, and until
    `OCR_IMPLEMENTED` says otherwise the honest answer is a refusal. Routing such a page to
    `LAYOUT` sent it to pdfplumber, which returned nothing — fine in a fully scanned document,
    which then fails loudly, and silent in a mixed one, where the readable pages carried the
    document to `ready` with the scanned ones absent from the index.
    """
    decision = decide(page("  \n \x0c "), ocr_available=True)

    assert decision.route is Route.UNREADABLE
    assert "OCR" in decision.reason


def test_the_ocr_route_is_reachable_once_an_engine_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal above is about this build, not about the rule.

    Without this test, wiring an engine and flipping the flag would be a change with no
    coverage of the path it turns on — and the routing rule itself, which M0 measured against
    thirteen documents, would look like it had been deleted rather than parked.
    """
    monkeypatch.setattr(routing, "OCR_IMPLEMENTED", True)

    decision = decide(page("  \n \x0c "), ocr_available=True)

    assert decision.route is Route.LAYOUT
    assert "OCR" in decision.reason


def test_without_ocr_that_page_is_unreadable_rather_than_empty() -> None:
    """The `low-spec` consequence, and the reason it is a refusal.

    An image-only PDF that ingests as "no content" appears in the list, gets asked about,
    and is answered from a different document. There is no symptom until the customer stops
    trusting the answers.
    """
    decision = decide(page(""), ocr_available=False)

    assert decision.route is Route.UNREADABLE
    assert "no OCR is available" in decision.reason


def test_a_few_stray_characters_do_not_count_as_a_text_layer() -> None:
    """An un-OCR'd scan usually carries a header stamp or a form field. Treating those as
    "has text" is how a 4,000-character page ingests as twelve characters of nothing."""
    decision = decide(page("Form 1040   Page 3"), ocr_available=True)

    assert decision.route is Route.UNREADABLE


def test_ordinary_prose_takes_the_fast_path() -> None:
    decision = decide(page(PROSE, single_column()), ocr_available=True)

    assert decision.route is Route.FAST
    assert decision.warnings == ()


def test_two_columns_are_detected_from_positions_not_from_text() -> None:
    """The failure §7 missed, and the dangerous one.

    Left-to-right extraction interleaves the columns, so every word is present and correct
    and the sentences are spliced together from two different places. Character counts,
    extraction ratios and Recall@8 all look healthy — only the meaning is wrong.
    """
    decision = decide(page(PROSE, columns()), ocr_available=True)

    assert decision.route is Route.LAYOUT
    assert "column" in decision.reason


def test_broken_word_spacing_is_reported_but_does_not_reroute() -> None:
    """The arXiv case: `densevectorindexofWikipedia`.

    A detector rather than a fix. We cannot repair the spacing here, and a page in this
    state contributes nothing to BM25 — every term is glued to its neighbours. It must not
    be indistinguishable from a clean page.
    """
    glued = " ".join(["densevectorindexofWikipediaandtheentirecorpus"] * 20 + ["ok"] * 30)

    decision = decide(page(glued, single_column()), ocr_available=True)

    assert any("spacing" in warning for warning in decision.warnings)


def test_the_decision_does_not_depend_on_the_profile_except_for_ocr() -> None:
    """The §11b rule, as an assertion.

    A profile may change how long a page takes. It may not change which parser reads a
    readable page, because that would change which text is stored, which changes what a
    query finds — and a flag that quietly changes what a user can see is a flag that causes
    a leak.
    """
    for text_of_page, words in ((PROSE, single_column()), (PROSE, columns())):
        with_ocr = decide(page(text_of_page, words), ocr_available=True)
        without = decide(page(text_of_page, words), ocr_available=False)

        assert with_ocr.route is without.route
        assert with_ocr.warnings == without.warnings
