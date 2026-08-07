"""Where a page's columns begin and end.

This is `routing.py`'s multi-column detector, generalised and moved so two callers can
share it. The detector answered one question — *is* this page multi-column — because that
was all routing needed. The extractor needs the other half of the same measurement: *where*
the gutters are, so it can read each column on its own.

Keeping them apart would mean two histograms of the same words with two sets of thresholds,
free to disagree about whether a page has columns at all. A page the router flags and the
extractor reads as one block is the failure this module exists to make impossible.

The constants are `routing.py`'s, unchanged, and they are worth respecting rather than
re-tuning: they were arrived at by measuring 1,842 real pages after two reasoned attempts
scored zero on the documents they were written to catch. The history is in `routing.py`'s
own comments.

Binning word **centres** rather than the spans they cover is the load-bearing detail. A
word's span smears across the trough; its centre does not, and that is the difference
between catching a two-column paper on 100% of pages and on none of them.
"""

from collections.abc import Sequence
from statistics import median

#: Resolution of the horizontal histogram. Sixty bins over a page is roughly a centimetre
#: each on A4 — fine enough to see a gutter, coarse enough that a ragged right edge does
#: not read as one.
BINS = 60

#: Where a gutter is allowed to be. A trough outside this band is a margin: text stops
#: there because the page does, which says nothing about columns.
CENTRAL_BAND = (0.30, 0.70)

#: How empty a bin must be, against the page's typical density, to count as a gutter.
TROUGH_RATIO = 0.25

#: Below this, a page is judged too sparse to read layout from. A title page or a page of
#: equations has empty bins from having little text, not from having columns, and judging
#: them produced a 73% false-positive rate on a single-column paper.
MIN_WORDS = 250


def histogram(centres: Sequence[float]) -> list[int]:
    """Word centres, binned across the normalised page width."""
    coverage = [0] * BINS
    for centre in centres:
        coverage[max(0, min(BINS - 1, int(centre * BINS)))] += 1
    return coverage


def gutters(centres: Sequence[float]) -> tuple[float, ...]:
    """The normalised x positions separating this page's columns, in reading order.

    Empty for a single-column page, one entry for two columns, two for three, and so on —
    the search is for every trough inside the body rather than for the single central one
    the yes/no detector needed.

    A trough is a *run* of low bins, and the split is placed at its middle: a gutter is
    physically wide, and cropping at its first low bin would clip the last few characters
    of the column before it.
    """
    if len(centres) < MIN_WORDS:
        return ()

    coverage = histogram(centres)
    peak = max(coverage, default=0)
    if not peak:
        return ()

    # The text block, without the margins. A page with a wide left margin would otherwise
    # have its emptiest slice outside the text entirely.
    body = [index for index, count in enumerate(coverage) if count > peak * 0.1]
    if len(body) < 10:
        return ()

    first, last = body[0], body[-1]
    inner = coverage[first : last + 1]
    typical = median(inner)
    if not typical:
        return ()

    # Only the middle of the block can hold a gutter — the same rule the yes/no detector
    # applies, expressed as a range of bins rather than a single slice.
    low = first + int(len(inner) * CENTRAL_BAND[0])
    high = first + int(len(inner) * CENTRAL_BAND[1])

    splits: list[float] = []
    run: list[int] = []
    for index in range(low, high + 1):
        if coverage[index] / typical < TROUGH_RATIO:
            run.append(index)
            continue
        if run:
            splits.append((run[0] + run[-1] + 1) / 2 / BINS)
            run = []
    if run:
        splits.append((run[0] + run[-1] + 1) / 2 / BINS)

    return tuple(splits)


def is_multi_column(centres: Sequence[float]) -> bool:
    """Whether this page's text is laid out in more than one column.

    Defined as "has at least one gutter" rather than measured separately, so the router and
    the extractor can never reach opposite conclusions about the same page.
    """
    return bool(gutters(centres))
