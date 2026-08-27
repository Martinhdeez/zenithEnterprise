"""Which parser reads which page, and what to do when neither can.

`technical-decisions.md` §7 routes per page rather than per document: a 1,500-page manual
is typically 40 pages of tables and 1,460 of running text, and paying the expensive parser
on all 1,500 is how ingestion takes hours instead of minutes.

M0 tested that rule against thirteen real documents and found **three** distinct extraction
failures, of which the rule catches one:

| Document | Failure | Caught by §7 |
|---|---|---|
| NASA scan | no text layer at all | yes |
| arXiv papers | missing spaces — `densevectorindexofWikipedia` | no |
| IRS forms | scrambled column order | no |

The IRS case is the dangerous one. Every word is present and correct, so character counts,
extraction ratios and Recall@8 all look healthy — only the meaning is wrong. This module
adds detectors for the two the rule missed, because a page that silently poisons the index
must not be indistinguishable from a clean one.
"""

from dataclasses import dataclass
from enum import Enum

from app.core.hardware import OCR_IMPLEMENTED
from app.features.ingestion.columns import is_multi_column
from app.features.ingestion.parsers.base import ParsedPage, Word

# Below this, the page has no usable text layer. Not zero: a scanned page often carries a
# few stray characters from a header stamp or a form field, and treating those as "has
# text" is how an un-OCR'd 4,000-character page ingests as 12 characters of nothing.
MIN_CHARACTERS = 100


# A run of this many characters with no whitespace is not a word. M0's arXiv pages produced
# `densevectorindexofWikipedia`; ordinary English tops out around 20 characters, and a URL
# or an identifier is caught by the ratio check below rather than by one long token.
LONG_RUN = 30
SUSPECT_RUN_RATIO = 0.02

# Multi-column detection lives in `columns.py`, along with the constants it was calibrated
# with and the history of how they were arrived at. It moved there when the extractor
# needed the other half of the same measurement — not just *whether* a page has columns but
# *where* they are, so it can read each one separately.
#
# Shared rather than duplicated on purpose: a page the router calls multi-column and the
# extractor reads as one block is precisely the inconsistency that would put interleaved
# text into the index while the warning said everything was handled.


class Route(Enum):
    FAST = "pdfplumber"
    LAYOUT = "docling"
    # No text and no way to get any. Not a parser choice — a refusal. See `pipeline.py`:
    # `status='ready'` with zero chunks is forbidden, so this ends the document in `failed`
    # with something an operator can act on.
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class Decision:
    route: Route
    reason: str
    warnings: tuple[str, ...] = ()


def decide(page: ParsedPage, *, ocr_available: bool) -> Decision:
    """Route one page.

    The decision is a pure function of the page and the profile's OCR availability, which
    is what lets a test assert that **every profile routes identically** except where OCR
    itself is unavailable. A profile may change how long a page takes; it may not change
    which document a query finds.
    """
    if len(page.text.strip()) < MIN_CHARACTERS:
        if ocr_available and OCR_IMPLEMENTED:
            return Decision(Route.LAYOUT, "no extractable text layer; needs OCR")
        return Decision(
            Route.UNREADABLE,
            "no extractable text layer, and no OCR is available to this installation",
        )

    warnings: list[str] = []
    if _has_broken_spacing(page.text):
        # A detector, not a fix. We cannot repair the spacing here, and a page in this
        # state is invisible to BM25 — every term is glued to its neighbours, so the
        # lexical half of hybrid search contributes nothing for it.
        warnings.append("suspect word spacing: lexical search will be weak on this page")

    if _looks_multi_column(page.words):
        return Decision(
            Route.LAYOUT,
            "multi-column layout: read column by column",
            tuple(warnings),
        )

    return Decision(Route.FAST, "text layer present, single column", tuple(warnings))


def _has_broken_spacing(text: str) -> bool:
    """Long unbroken runs, as a proportion of tokens.

    A proportion rather than a count: one long token is a URL, and a page of them is a
    parser that dropped the spaces. The threshold is deliberately loose — this raises a
    warning, not a failure, and a false positive costs a line in a support report.
    """
    tokens = text.split()
    if not tokens:
        return False
    long_runs = sum(1 for token in tokens if len(token) >= LONG_RUN)
    return long_runs / len(tokens) > SUSPECT_RUN_RATIO


def _looks_multi_column(words: tuple[Word, ...]) -> bool:
    """A pronounced trough in the horizontal distribution of text.

    The signal M0 proved §7 was missing, detected on positions rather than on text because
    the text is exactly what gives no hint.

    Now a thin wrapper over `columns.py`, which the extractor also uses to decide where to
    crop. The measurement is unchanged — the constants and the reasoning behind them moved
    with it — but there is one of it instead of two.

    Still worth routing on after the extractor learned to read columns: a page whose columns
    were read separately is *handled*, not simple, and the record of which pages needed
    that is what makes a later extraction problem findable.
    """
    return is_multi_column([(word.box.x0 + word.box.x1) / 2 for word in words])
