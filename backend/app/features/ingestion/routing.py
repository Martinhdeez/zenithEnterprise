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

# Two column clusters have to be separated by real whitespace to count. A gap narrower than
# this is ordinary word spacing in a wide line.
COLUMN_GAP = 0.08
MIN_WORDS_FOR_LAYOUT = 40


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
    """Two horizontal bands of text with a clear gutter between them.

    The signal M0 proved §7 was missing. pdfplumber reads a two-column page left to right
    across the full width, so a line from the left column is followed by a line from the
    right — the words are all correct and the sentences are spliced together from two
    different places.

    Detected on positions rather than on text, because the text is exactly what gives no
    hint. The heuristic is deliberately conservative: it looks for one wide vertical gutter
    that no word crosses, which a table with narrow columns will not produce.
    """
    if len(words) < MIN_WORDS_FOR_LAYOUT:
        return False

    # Walk the page left to right and find the widest span no word occupies.
    spans = sorted((word.box.x0, word.box.x1) for word in words)
    gutter = (0.0, 0.0)
    reach = spans[0][1]
    for start, end in spans[1:]:
        if start > reach and start - reach > gutter[1] - gutter[0]:
            gutter = (reach, start)
        reach = max(reach, end)

    if gutter[1] - gutter[0] < COLUMN_GAP:
        return False

    # A gutter only means columns if there is substantial text on **both** sides of it. A
    # wide margin beside one narrow paragraph is not a two-column page, and rerouting it
    # to the expensive parser for nothing is the cost §7 exists to avoid.
    left = sum(1 for word in words if word.box.x1 <= gutter[0])
    right = sum(1 for word in words if word.box.x0 >= gutter[1])
    return min(left, right) / len(words) > 0.25
