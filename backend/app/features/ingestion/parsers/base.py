"""What every parser has to return, and why the shape is what it is.

Two parsers exist in the design: `pdfplumber` for the common case and Docling for pages
that need layout understanding. They are behind one protocol so routing can choose per page
without the pipeline knowing which one ran.

**Bounding boxes are not optional.** They are what makes highlighting a citation in the PDF
viewer reliable; the alternative — matching character offsets against pdf.js's text layer —
is fragile and produces misaligned highlights. They exist only during parsing, so omitting
them forces a re-parse of the whole corpus rather than a re-embed. M0 priced a re-parse at
roughly five minutes per hundred pages: eight hours for a ten-thousand-page corpus.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Box:
    """One rectangle on one page, normalised to 0–1.

    Normalised rather than absolute so the coordinates survive zoom, a viewer that renders
    at a different scale, and a page size we did not anticipate. An absolute box is correct
    exactly once, on the machine that produced it.
    """

    page: int
    x0: float
    y0: float
    x1: float
    y1: float

    def as_dict(self) -> dict[str, float | int]:
        return {"page": self.page, "x0": self.x0, "y0": self.y0, "x1": self.x1, "y1": self.y1}


@dataclass(frozen=True, slots=True)
class Word:
    """A word with its place on the page.

    The pipeline needs positions to build chunk boxes, and the router needs them to see the
    layout — a page whose words form two separated columns is read wrongly by every
    left-to-right extractor, and nothing in the resulting *text* shows it.
    """

    text: str
    box: Box


@dataclass(frozen=True, slots=True)
class ParsedPage:
    page_num: int
    text: str
    words: tuple[Word, ...] = ()
    # `pdfplumber` | `docling`. Recorded per page because routing is per page: a document
    # is usually a handful of hard pages in a pile of easy ones.
    method: str = "pdfplumber"
    # Non-fatal observations about this page's extraction. M0 found a failure mode where
    # every word is present and correct and the text is still wrong; a page that silently
    # poisons the lexical index must not look identical to a clean one.
    warnings: tuple[str, ...] = field(default=())


class Parser(Protocol):
    name: str

    def parse(self, path: Path) -> list[ParsedPage]: ...
