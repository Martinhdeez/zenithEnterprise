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
"""

from pathlib import Path

import pdfplumber

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

        words = tuple(
            Word(
                text=str(word["text"]),
                box=Box(
                    page=page_num,
                    # Normalised here rather than at the point of use, so no consumer can
                    # forget. A box in absolute points is correct exactly once — on the
                    # page size that produced it.
                    x0=float(word["x0"]) / width,
                    y0=float(word["top"]) / height,
                    x1=float(word["x1"]) / width,
                    y1=float(word["bottom"]) / height,
                ),
            )
            for word in page.extract_words() or []  # type: ignore[attr-defined]
        )

        return ParsedPage(
            page_num=page_num,
            # `extract_text` rather than joining the words: it applies pdfplumber's own
            # line and space reconstruction, which is better than anything we would write
            # here. The words are kept alongside for positions, not for the text.
            text=page.extract_text() or "",  # type: ignore[attr-defined]
            words=words,
            method=self.name,
        )
