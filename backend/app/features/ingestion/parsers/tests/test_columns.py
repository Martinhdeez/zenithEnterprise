"""Reading a two-column page in the order a person reads it.

`extract_text` rebuilds lines in horizontal bands across the whole page, so on two columns
it walks out of the left one, across the gutter, and into the right — returning a line
spliced from both. Every word is present and correct and the sentence never existed, which
is why this is asserted against real documents rather than a synthetic fixture: the failure
is a property of how real papers are typeset, and a fixture would only prove the fixture.

`bert-paper.pdf` is the two-column case (NAACL, flagged on 100% of its pages) and
`gdpr.pdf` the single-column control — the path that must not have changed at all.
"""

from pathlib import Path

import pytest

from app.features.ingestion.columns import gutters, is_multi_column
from app.features.ingestion.parsers.base import ParsedPage
from app.features.ingestion.parsers.pdfplumber_parser import PdfPlumberParser

CORPUS = Path(__file__).resolve().parents[5] / "eval" / "documents"

pytestmark = pytest.mark.skipif(
    not (CORPUS / "bert-paper.pdf").exists(),
    reason="the evaluation corpus is not checked out",
)


@pytest.fixture(scope="module")
def two_column() -> list[ParsedPage]:
    return PdfPlumberParser().parse(CORPUS / "bert-paper.pdf")


def test_a_sentence_is_not_spliced_from_two_columns(two_column: list[ParsedPage]) -> None:
    """The failure this whole change exists for, named exactly.

    Before column-aware extraction, page 4 produced "representation is able to unambiguously
    represent tokens at random, and then predict those masked" — the first half from the
    left column, the second from the right, describing something the paper never said. The
    real continuation is what has to follow it.
    """
    text = two_column[3].text

    assert "representation is able to unambiguously represent" in text
    spliced = "unambiguously represent tokens at random"
    assert spliced not in text, "a line was still assembled across the gutter"


def test_reading_order_follows_the_column_down(two_column: list[ParsedPage]) -> None:
    """A paragraph has to arrive contiguous. Interleaved extraction breaks it into
    alternating fragments, which is invisible in a character count and fatal to the
    embedding built from it."""
    text = two_column[3].text.replace("\n", " ")

    assert "our input representation is able to unambiguously represent" in text


def test_every_box_stays_inside_its_column(two_column: list[ParsedPage]) -> None:
    """The second symptom: highlights that spanned both columns.

    Words now come from a cropped page, so a box is bounded by the column that produced it
    by construction. Asserted against the gutter the detector itself found, rather than a
    hardcoded midpoint, so the test cannot pass by agreeing with a wrong assumption about
    where the columns are.
    """
    page = two_column[3]
    splits = gutters([(word.box.x0 + word.box.x1) / 2 for word in page.words])
    assert splits, "the fixture page should be detected as multi-column"

    gutter = splits[0]
    straddling = [word for word in page.words if word.box.x0 < gutter < word.box.x1]
    assert not straddling, f"{len(straddling)} word boxes cross the gutter at {gutter}"


def test_a_single_column_page_is_not_split() -> None:
    """Cropping a page with no gutter would introduce error rather than remove it, so the
    single-column path has to stay the untouched one."""
    pages = PdfPlumberParser().parse(CORPUS / "gdpr.pdf")
    page = pages[20]

    assert not is_multi_column([(word.box.x0 + word.box.x1) / 2 for word in page.words])
    assert page.text.strip(), "the control document still extracts"


def test_the_detector_and_the_extractor_agree(two_column: list[ParsedPage]) -> None:
    """They read the same histogram through one module, and this is what that buys.

    A page the router calls multi-column and the extractor reads as one block would put
    interleaved text in the index while the warning claimed it was handled — the one
    inconsistency `columns.py` exists to make impossible.
    """
    for page in two_column:
        centres = [(word.box.x0 + word.box.x1) / 2 for word in page.words]
        assert is_multi_column(centres) == bool(gutters(centres))
