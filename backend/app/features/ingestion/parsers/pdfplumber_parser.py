# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportMissingTypeStubs=false, reportUnknownArgumentType=false
#
# pdfplumber ships no stubs. Suppressed for this file only; nothing else under `app/`
# relaxes strictness.

"""The fast path, and the one that runs on every profile.

pdfplumber is MIT, pure Python, and reads a text layer in milliseconds. M0 measured what it
gets wrong — missing spaces on arXiv papers, nothing at all on un-OCR'd scans, scrambled
column order on IRS forms — and those findings live in `routing.py`, which decides when
this parser is not enough. This module's job is to extract honestly and report what it saw.

**Columns are read one at a time.** `extract_text` reconstructs lines in horizontal bands
across the whole page, so on a two-column layout it walks out of the left column, across
the gutter and into the right one, and returns a line spliced from both. Measured on
`bert-paper.pdf` page 4: eleven of its 117 lines were sentences that appear nowhere in the
document — "representation is able to unambiguously represent" (left column) welded to
"tokens at random, and then predict those masked" (right). That text was what got embedded,
and the boxes drawn from it spanned the full page width, so a citation highlighted a band
across both columns instead of the paragraph it came from.

The fix is to hand `extract_text` one column at a time. Cropping is what makes it correct
rather than merely better: pdfplumber's line reconstruction is good, and the problem was
never the reconstruction — it was being given a page whose lines genuinely do span two
columns. Given one column, it has nothing to splice.
"""

from pathlib import Path

import pdfplumber

from app.features.ingestion.columns import gutters
from app.features.ingestion.parsers.base import Box, ParsedPage, Word
from app.features.ingestion.parsers.tables import is_table, to_markdown

# Below this many spaces per character, a page is assumed to have come out glued rather
# than to be genuinely sparse. Measured across the thirteen documents in `eval/documents`:
# healthy pages sit between **0.108 and 0.204**, and the two LaTeX papers whose spaces were
# lost sit at **0.028 and 0.034**. The threshold is above the broken band and below every
# healthy median, and it only decides whether the second extraction is *attempted* — what
# decides whether it is kept is the improvement below.
GLUED_SPACE_RATIO = 0.12

# pdfplumber's default `x_tolerance` is 3 points, which merges words in tightly kerned
# fonts. `WeusedtheAdamoptimizer[20]withβ =0.9` is a real passage from
# `attention-is-all-you-need.pdf` as this parser used to read it: no word on that page was
# searchable, the embedding was of a run-on blob, and the passage was unreadable in the
# viewer. 1.5 recovers it as "We used the Adam optimizer [20] with β = 0.9".
TIGHT_X_TOLERANCE = 1.5

# How much better the retry has to be before it is believed. Measured on the same corpus:
# the two broken documents improve 5.16x and 4.16x, a third partially glued one 1.43x, and
# every healthy document 1.00x-1.03x. A gate at 1.25 separates them with room on both sides.
#
# The gate is the point. A page that is legitimately sparse — a form, a table of figures, a
# language that does not space words — gains nothing from a tighter tolerance and keeps the
# default, so this can rescue a broken page and cannot damage a working one.
RETRY_IMPROVEMENT = 1.25


def space_ratio(text: str) -> float:
    return text.count(" ") / len(text) if text else 0.0


def frame(page: object, width: float, height: float) -> tuple[float, float, float, float]:
    """The page's own rectangle: `(left, top, right, bottom)` in absolute points.

    Read from the page rather than assumed to be `(0, 0, width, height)`. A mediabox is not
    obliged to start at the origin and scanned documents routinely do not —
    `nasa-scanned-report.pdf` starts at x0=1.43921. Two things broke because of that
    assumption, and both failed the *whole document* rather than the page: a crop taken
    from 0 is outside the parent and pdfplumber refuses it, and a coordinate normalised by
    dividing by the width alone lands outside `[0, 1]` and puts a citation highlight in the
    wrong place.
    """
    left, top, right, bottom = (
        float(value) for value in getattr(page, "bbox", (0.0, 0.0, width, height))
    )
    # A degenerate box would divide by zero downstream; fall back to the reported size.
    if right - left <= 0 or bottom - top <= 0:
        return 0.0, 0.0, width, height
    return left, top, right, bottom


def page_count(path: Path) -> int:
    """How many pages, without decoding any of them.

    `pdfplumber.open` reads the page tree and nothing else; the expensive work happens the
    first time a page's characters are touched. So this is the cheap question to ask before
    deciding whether to ask the expensive one, which is what makes a page limit enforceable
    rather than merely configured.
    """
    with pdfplumber.open(path) as document:
        return len(document.pages)


class PdfPlumberParser:
    name = "pdfplumber"

    def parse(self, path: Path) -> list[ParsedPage]:
        pages: list[ParsedPage] = []
        with pdfplumber.open(path) as document:
            for index, page in enumerate(document.pages, start=1):
                pages.append(self._page(page, index))
                # Released per page, and this is not a micro-optimisation. pdfplumber
                # caches every character, line and rectangle it decoded on the page object
                # and keeps it for the lifetime of the document — so parsing a long file
                # accumulates the whole decoded corpus in memory whether anything still
                # needs it or not.
                #
                # Measured on `infrastructure-act.pdf` (1,039 pages): 3,068 MB without this
                # call, 58 MB with it. The worker then sat at ~2 GB *after* finishing,
                # because the caches outlived the parse, and TEI — which needs 4.6 GB to
                # serve BGE-M3 — was OOM-killed by the sum on an 8 GB machine. Two large
                # documents failed to ingest that way before anyone looked at why.
                page.close()  # type: ignore[attr-defined]
        return pages

    def _spacing(self, page: object) -> tuple[dict[str, float], str]:
        """The word-splitting tolerance this page needs, and the text that decided it.

        The tolerance is returned as keyword arguments so every extraction on the page — the
        text, the words behind the citation boxes, each column, each band between tables —
        uses the same one. They must agree: `_boxes_for` walks the word list forward through
        the text looking for each word in turn, so text split one way and words split
        another would match nothing and every citation would highlight an empty page.

        **The text comes back with it**, because deciding required extracting it and the
        single-column path wants exactly that string. Returning only the tolerance left the
        caller re-extracting a page that had just been extracted.

        Not for speed — that was checked rather than assumed, and it is not there.
        pdfplumber caches the decoded characters on the page object, so the second call
        costs almost nothing: 3.95 s against 3.93 s over `gdpr.pdf`'s 88 pages, which is
        noise. What it buys is one obvious extraction per page instead of two that have to
        agree, in a method whose whole purpose is that every read of the page agrees.
        """
        text = page.extract_text() or ""  # type: ignore[attr-defined]
        # Too short to judge. A cover page or a mostly-blank one has no stable ratio, and
        # guessing from forty characters is how a working page gets "rescued" into a worse
        # one.
        if len(text) < 200 or space_ratio(text) >= GLUED_SPACE_RATIO:
            return {}, text

        tighter = page.extract_text(x_tolerance=TIGHT_X_TOLERANCE) or ""  # type: ignore[attr-defined]
        if space_ratio(tighter) < space_ratio(text) * RETRY_IMPROVEMENT:
            return {}, text
        return {"x_tolerance": TIGHT_X_TOLERANCE}, tighter

    def _page(self, page: object, page_num: int) -> ParsedPage:
        width = float(getattr(page, "width", 0)) or 1.0
        height = float(getattr(page, "height", 0)) or 1.0

        box = frame(page, width, height)
        spacing, whole = self._spacing(page)
        raw = page.extract_words(**spacing) or []  # type: ignore[attr-defined]

        grids = self._tables(page)
        if grids:
            # Tables win over column splitting, and they have to. A table spanning the full
            # width of a two-column page puts words in the gutter, which is exactly the
            # evidence `gutters` reads — so a page with a wide table often looks
            # single-column, and a page with a narrow one looks like it has a gutter where
            # the table's own middle rule is. Neither reading helps, and cropping a table
            # down the middle destroys the rows this path exists to preserve.
            return self._with_tables(page, page_num, box, grids, raw, spacing)

        centres = [
            ((float(word["x0"]) + float(word["x1"])) / 2 - box[0]) / (box[2] - box[0])
            for word in raw
        ]
        splits = gutters(centres)

        if not splits:
            # Single column: the whole page, exactly as before. Cropping a page that has no
            # gutter would be a way to introduce error, not remove it — and this is the text
            # `_spacing` already extracted at the tolerance it chose, so the common page
            # costs one extraction rather than two.
            text = whole
            words = tuple(self._word(word, page_num, box) for word in raw)
            return ParsedPage(page_num=page_num, text=text, words=words, method=self.name)

        return self._by_column(page, page_num, box, splits, spacing)

    def _tables(self, page: object) -> list[tuple[tuple[float, float], str]]:
        """The page's real tables, each as its vertical extent and its Markdown.

        `find_tables` rather than `extract_tables`, because the position is half of what is
        needed: a table has to go back into the text where it was, and `extract_tables`
        returns the cells and throws the geometry away.

        Anything that fails `is_table` is dropped here and its region is left to ordinary
        text extraction, so a page border mistaken for a grid costs nothing at all.
        """
        found: list[tuple[tuple[float, float], str]] = []
        for table in page.find_tables() or []:  # type: ignore[attr-defined]
            rows = table.extract() or []
            if not is_table(rows):
                continue
            _, top, _, bottom = table.bbox
            found.append(((float(top), float(bottom)), to_markdown(rows)))
        return sorted(found, key=lambda item: item[0][0])

    def _with_tables(
        self,
        page: object,
        page_num: int,
        box: tuple[float, float, float, float],
        grids: list[tuple[tuple[float, float], str]],
        raw: list[object],
        spacing: dict[str, float],
    ) -> ParsedPage:
        """The page read in vertical bands: prose, table, prose, table, prose.

        Injection rather than appending. Appending the Markdown to the end of the page would
        leave the flattened version of the same table in the prose above it, and the two
        disagree — the flattened one has lost which number belongs to which column, so the
        chunk would contain both a correct table and a scrambled one, and nothing tells the
        model which to believe. Reading the non-table bands and dropping the table bands
        means each value appears exactly once, in the form that keeps its meaning.

        The words are the page's own, untouched and in reading order. `_boxes_for` walks
        them forward through the text looking for each one in turn, so the pipes and dashes
        between them are simply skipped, and a citation into a table still highlights the
        rows it came from.
        """
        # The page's own frame, not `(0, 0, width, height)`. A mediabox is not obliged to
        # start at the origin, and scanned documents routinely do not: `nasa-scanned-report`
        # begins at x0=1.43921, and cropping from 0 raises
        #
        #   ValueError: Bounding box (0.0, 0, …) is not fully within parent page bounding box
        #
        # which fails the whole document rather than the page. Reading the bounds from the
        # page means the bands are always inside it whatever the producer chose.
        left, top_edge, right, bottom_edge = box
        pieces: list[str] = []
        cursor = top_edge

        for (top, bottom), markdown in grids:
            # Clamped as well as framed: `find_tables` reports a table's own geometry, and
            # a rule drawn fractionally outside the mediabox would put the band's edge
            # outside it too.
            edge = min(max(top, top_edge), bottom_edge)
            if edge > cursor:
                band = page.crop((left, cursor, right, edge))  # type: ignore[attr-defined]
                above = (band.extract_text(**spacing) or "").strip()
                if above:
                    pieces.append(above)
            pieces.append(markdown)
            cursor = min(max(cursor, bottom), bottom_edge)

        if cursor < bottom_edge:
            band = page.crop((left, cursor, right, bottom_edge))  # type: ignore[attr-defined]
            below = (band.extract_text(**spacing) or "").strip()
            if below:
                pieces.append(below)

        return ParsedPage(
            page_num=page_num,
            # A blank line between bands: prose and a table are not continuous, and the
            # chunker treats "\n\n" as the strongest break it can cut on — which is what
            # keeps a table from being split down the middle when it can be avoided.
            text="\n\n".join(pieces),
            words=tuple(self._word(word, page_num, box) for word in raw),
            method=self.name,
            # No warning. `warnings` means "this text is suspect" — `_summarise` turns them
            # into a document-level "N of M pages extracted with warnings" that a user
            # reads as damage — and a rendered table is the opposite of damage. It is also
            # moot: `_routed` replaces the parser's warnings with the router's.
        )

    def _by_column(
        self,
        page: object,
        page_num: int,
        box: tuple[float, float, float, float],
        splits: tuple[float, ...],
        spacing: dict[str, float],
    ) -> ParsedPage:
        """Read each column separately and concatenate in reading order.

        The words come from the crops rather than from the whole page, so a box is bounded
        by the column that produced it by construction — there is no rule to enforce and
        nothing to clamp afterwards.

        Columns are joined with a blank line. They are not continuous prose: the last
        sentence of the left column and the first of the right are usually unrelated, and
        running them together would hand the chunker a sentence boundary that does not
        exist.
        """
        page_left, page_top, page_right, page_bottom = box
        span = page_right - page_left
        bounds = [0.0, *splits, 1.0]
        texts: list[str] = []
        words: list[Word] = []

        for left, right in zip(bounds, bounds[1:], strict=False):
            # The split fractions are of the page's own width, so they are mapped back into
            # its own rectangle. `(left * width, 0, ...)` assumed both origins were zero and
            # raised on any page whose mediabox is offset — the crash that failed
            # `nasa-scanned-report.pdf` in full.
            column = page.crop(  # type: ignore[attr-defined]
                (page_left + left * span, page_top, page_left + right * span, page_bottom)
            )
            text = (column.extract_text(**spacing) or "").strip()
            if text:
                texts.append(text)
            # `crop` keeps the original page's coordinate space, so these are already in
            # the same frame as an uncropped read — no offset to add back.
            words.extend(
                self._word(word, page_num, box) for word in column.extract_words(**spacing) or []
            )

        return ParsedPage(
            page_num=page_num,
            text="\n\n".join(texts),
            words=tuple(words),
            method=self.name,
        )

    def _word(self, word: object, page_num: int, box: tuple[float, float, float, float]) -> Word:
        left, top, right, bottom = box
        span, extent = right - left, bottom - top
        return Word(
            text=str(word["text"]),  # type: ignore[index]
            box=Box(
                page=page_num,
                # Normalised here rather than at the point of use, so no consumer can
                # forget. A box in absolute points is correct exactly once — on the page
                # size that produced it. Relative to the page's own origin, not to zero,
                # or a page whose mediabox starts at x0=1.44 puts every highlight 0.2% out.
                x0=(float(word["x0"]) - left) / span,  # type: ignore[index]
                y0=(float(word["top"]) - top) / extent,  # type: ignore[index]
                x1=(float(word["x1"]) - left) / span,  # type: ignore[index]
                y1=(float(word["bottom"]) - top) / extent,  # type: ignore[index]
            ),
        )
