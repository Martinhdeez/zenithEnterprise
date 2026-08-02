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
from statistics import median

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

# A column gutter is a *low-density* band, not an empty one, and it is found by where words
# sit rather than by where they stop.
#
# Two earlier versions were measured and discarded, which is the only reason these numbers
# are trustworthy. Looking for a strip no word crossed fired on **nothing** across 1,842
# pages: every real page has a header, a page number or a figure caption spanning the full
# width, and one of those closes the gap. Counting the bins each word *spans* was better but
# smeared the trough, catching M0's IRS document at 59% of pages and the genuinely
# two-column BERT paper at 0%.
#
# Binning word **centres** is what separates them. Measured across the corpus:
#
#   bert-paper (two-column, NAACL)                    100% of pages flagged
#   irs-1040-instructions (M0's scrambled document)    73%
#   gdpr, eu-ai-act, dsa, infrastructure-act, rag       0%
#   boe-monetary-policy, nasa-technical-report        16%, 14% — false positives
#
# A false positive costs one warning on a page that is fine. A false negative puts
# interleaved text into the index where nothing can see it. The threshold leans towards the
# cheaper mistake.
BINS = 60
CENTRAL_BAND = (0.30, 0.70)
TROUGH_RATIO = 0.25

# Only text-dense pages are judged. A sparse page — a title page, a figure, a page of
# equations — has empty bins from sparsity rather than from layout, and judging it produced
# a 73% false-positive rate on a single-column paper. Below this, the page is left alone.
MIN_WORDS_FOR_LAYOUT = 250


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
        if ocr_available:
            return Decision(Route.LAYOUT, "no extractable text layer; needs OCR")
        return Decision(
            Route.UNREADABLE,
            "no extractable text layer, and OCR is disabled on this hardware profile",
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
            "multi-column layout: left-to-right extraction interleaves the columns",
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

    The signal M0 proved §7 was missing. pdfplumber reads a two-column page left to right
    across the full width, so a line from the left column is followed by a line from the
    right, and the sentences are spliced together from two different places. Detected on
    positions rather than on text, because the text is exactly what gives no hint.

    Measured on the corpus rather than reasoned about — twice, because the first two
    reasoned versions scored zero and near-zero on the documents this exists to catch. The
    margins are excluded before the comparison: a page with a wide left margin would
    otherwise have its lowest-density slice outside the text entirely, which says nothing
    about columns.
    """
    if len(words) < MIN_WORDS_FOR_LAYOUT:
        return False

    coverage = [0] * BINS
    for word in words:
        centre = (word.box.x0 + word.box.x1) / 2
        coverage[max(0, min(BINS - 1, int(centre * BINS)))] += 1

    peak = max(coverage)
    body = [index for index, count in enumerate(coverage) if count > peak * 0.1]
    if len(body) < 10:
        return False

    inner = coverage[body[0] : body[-1] + 1]
    start = body[0] + int(len(inner) * CENTRAL_BAND[0])
    end = body[0] + int(len(inner) * CENTRAL_BAND[1])
    typical = median(inner)
    if not typical:
        return False

    # Only the middle of the text block is considered. A trough at the edge is a margin;
    # a trough in the middle, with dense text on both sides of it, is a gutter.
    return min(coverage[start : end + 1]) / typical < TROUGH_RATIO
