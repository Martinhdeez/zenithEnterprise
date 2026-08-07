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


class PdfPlumberParser:
    name = "pdfplumber"

    def parse(self, path: Path) -> list[ParsedPage]:
        pages: list[ParsedPage] = []
        with pdfplumber.open(path) as document:
            for index, page in enumerate(document.pages, start=1):
                pages.append(self._page(page, index))
        return pages

    def _page(self, page: object, page_num: int) -> ParsedPage:
        width = float(getattr(page, "width", 0)) or 1.0
        height = float(getattr(page, "height", 0)) or 1.0

        raw = page.extract_words() or []  # type: ignore[attr-defined]
        centres = [(float(word["x0"]) + float(word["x1"])) / 2 / width for word in raw]
        splits = gutters(centres)

        if not splits:
            # Single column: hand the whole page to pdfplumber, exactly as before. Cropping
            # a page that has no gutter would be a way to introduce error, not remove it.
            text = page.extract_text() or ""  # type: ignore[attr-defined]
            words = tuple(self._word(word, page_num, width, height) for word in raw)
            return ParsedPage(page_num=page_num, text=text, words=words, method=self.name)

        return self._by_column(page, page_num, width, height, splits)

    def _by_column(
        self,
        page: object,
        page_num: int,
        width: float,
        height: float,
        splits: tuple[float, ...],
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
        bounds = [0.0, *splits, 1.0]
        texts: list[str] = []
        words: list[Word] = []

        for left, right in zip(bounds, bounds[1:], strict=False):
            column = page.crop((left * width, 0, right * width, height))  # type: ignore[attr-defined]
            text = (column.extract_text() or "").strip()
            if text:
                texts.append(text)
            # `crop` keeps the original page's coordinate space, so these are already in
            # the same frame as an uncropped read — no offset to add back.
            words.extend(
                self._word(word, page_num, width, height) for word in column.extract_words() or []
            )

        return ParsedPage(
            page_num=page_num,
            text="\n\n".join(texts),
            words=tuple(words),
            method=self.name,
        )

    def _word(self, word: object, page_num: int, width: float, height: float) -> Word:
        return Word(
            text=str(word["text"]),  # type: ignore[index]
            box=Box(
                page=page_num,
                # Normalised here rather than at the point of use, so no consumer can
                # forget. A box in absolute points is correct exactly once — on the page
                # size that produced it.
                x0=float(word["x0"]) / width,  # type: ignore[index]
                y0=float(word["top"]) / height,  # type: ignore[index]
                x1=float(word["x1"]) / width,  # type: ignore[index]
                y1=float(word["bottom"]) / height,  # type: ignore[index]
            ),
        )
