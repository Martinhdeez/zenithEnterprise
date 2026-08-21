"""The parser against a real PDF with a real ruled table.

`test_tables.py` covers the rendering in isolation, on grids handed to it directly. This
file is the part that can only be proved end to end: that pdfplumber *finds* the table on a
drawn page, that the Markdown lands in `ParsedPage.text` where the chunker will pick it up,
and — the assertion that would otherwise be missed — that the flattened version of the same
table is **not** also there. Both present is worse than neither: the chunk would hold a
correct table and a scrambled copy of it, and nothing tells the model which to read.
"""

import io
from pathlib import Path

import pytest

from app.features.ingestion.parsers.pdfplumber_parser import PdfPlumberParser

GRID = [
    ["Region", "2024", "2025"],
    ["North", "1204", "1391"],
    ["South", "890", "774"],
]


def table_pdf(path: Path, intro: str = "", outro: str = "") -> Path:
    """A page with a ruled table on it, drawn rather than checked in.

    Ruled, with visible lines, because that is what `find_tables` looks for by default —
    a borderless table is found by text alignment and is a different, much less reliable
    case that this parser deliberately does not chase.
    """
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    from reportlab.platypus import Table

    buffer = io.BytesIO()
    drawing = canvas.Canvas(buffer, pagesize=letter)
    if intro:
        drawing.drawString(72, 740, intro)
    table = Table(GRID, colWidths=[100, 100, 100], rowHeights=[24] * len(GRID))
    table.setStyle([("GRID", (0, 0), (-1, -1), 1, (0, 0, 0))])
    table.wrapOn(drawing, 400, 200)
    table.drawOn(drawing, 72, 600)
    if outro:
        drawing.drawString(72, 560, outro)
    drawing.showPage()
    drawing.save()
    path.write_bytes(buffer.getvalue())
    return path


@pytest.fixture
def parsed(tmp_path: Path) -> str:
    pdf = table_pdf(
        tmp_path / "financials.pdf",
        intro="Billings by region, in thousands.",
        outro="Figures are unaudited.",
    )
    pages = PdfPlumberParser().parse(pdf)
    return pages[0].text


def test_the_table_arrives_as_markdown(parsed: str) -> None:
    assert "| Region | 2024 | 2025 |" in parsed
    assert "| --- | --- | --- |" in parsed
    assert "| South | 890 | 774 |" in parsed


def test_the_flattened_copy_is_gone(parsed: str) -> None:
    """The reason the table's band is dropped from the text rather than the Markdown being
    appended to it. Two versions of one table in one chunk is a contradiction the model
    resolves by guessing."""
    assert "South 890 774" not in parsed
    assert parsed.count("774") == 1


def test_the_prose_around_the_table_is_kept(parsed: str) -> None:
    """Only the table's own band is replaced. A page is not a table, and dropping the
    sentence that says what the numbers are would cost more than the grid is worth."""
    assert "Billings by region, in thousands." in parsed
    assert "Figures are unaudited." in parsed


def test_reading_order_survives(parsed: str) -> None:
    assert parsed.index("Billings by region") < parsed.index("| Region")
    assert parsed.index("| South") < parsed.index("Figures are unaudited.")


def test_a_page_with_no_table_is_untouched(tmp_path: Path) -> None:
    """The regression guard for every ordinary page in the corpus.

    Table detection runs on all of them, so a page without one has to come out of the
    single-column path exactly as it did before — no Markdown, no banding, no blank lines
    that were not there.
    """
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    drawing = canvas.Canvas(buffer, pagesize=letter)
    drawing.drawString(72, 720, "The controller shall implement appropriate measures.")
    drawing.showPage()
    drawing.save()
    plain = tmp_path / "plain.pdf"
    plain.write_bytes(buffer.getvalue())

    text = PdfPlumberParser().parse(plain)[0].text

    assert text.strip() == "The controller shall implement appropriate measures."


def test_the_words_still_carry_boxes_for_the_table(tmp_path: Path) -> None:
    """Citations into a table still have to highlight in the viewer.

    The words are the page's own and unmodified — `_boxes_for` walks them forward through
    the text, so the pipes and dashes between them are skipped rather than breaking the
    match. Without this the panel would render a table and highlight nothing.
    """
    pages = PdfPlumberParser().parse(table_pdf(tmp_path / "grid.pdf"))
    values = {word.text for word in pages[0].words}

    assert {"Region", "South", "774"} <= values
    assert all(0.0 <= word.box.x0 <= 1.0 for word in pages[0].words)


def test_a_page_whose_box_does_not_start_at_the_origin(tmp_path: Path) -> None:
    """The bug this file did not catch until a real scanned report hit it.

    `_with_tables` used to read its bands as `(0, cursor, width, top)`, which assumes the
    mediabox starts at the origin. Nothing in the format promises that, and scanners
    routinely produce pages that do not — `nasa-scanned-report.pdf` starts at x0=1.43921.
    pdfplumber refuses a crop that falls outside its parent:

        ValueError: Bounding box (0.0, 0, …) is not fully within parent page bounding box

    Raised during ingestion, so the whole document failed rather than the page, and the
    corpus silently lost a file. The bands are now read from the page's own `bbox`.
    """
    source = table_pdf(tmp_path / "grid.pdf", intro="Billings by region", outro="Unaudited.")

    # The mediabox is shifted by patching the bytes rather than by rewriting the file with
    # another library: `[0 0 612 792]` -> `[1 0 612 792]` is the same length, so every
    # offset in the xref table stays valid and the document needs no repair.
    raw = source.read_bytes()
    assert b"/MediaBox [ 0 0 612 792 ]" in raw, "reportlab's mediabox is not where we expect"
    shifted = tmp_path / "offset.pdf"
    shifted.write_bytes(raw.replace(b"/MediaBox [ 0 0 612 792 ]", b"/MediaBox [ 1 0 612 792 ]"))

    pages = PdfPlumberParser().parse(shifted)  # must not raise

    assert pages
    assert "| Region" in pages[0].text, "the table is still found and rendered"
