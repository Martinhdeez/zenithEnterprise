"""Tables as Markdown, and the grids that must not become tables.

The failure this exists to prevent is not a crash. A financial table flattened by
`extract_text` is a run of numbers with the columns gone, and every downstream component
treats it as ordinary prose: it embeds cleanly, it retrieves plausibly, and the model
answers from it with a number that belongs to the wrong year. Nothing in the system reports
a problem, because nothing in the system can see one.
"""

from app.features.ingestion.parsers.tables import cell, is_table, rendered, to_markdown

GRID = [
    ["Region", "2024", "2025"],
    ["North", "1,204", "1,391"],
    ["South", "890", "774"],
]


def test_a_grid_becomes_a_markdown_table() -> None:
    markdown = to_markdown(GRID)

    assert markdown.splitlines() == [
        "| Region | 2024 | 2025 |",
        "| --- | --- | --- |",
        "| North | 1,204 | 1,391 |",
        "| South | 890 | 774 |",
    ]


def test_every_value_stays_under_its_own_header() -> None:
    """The property the whole feature is for, stated as a lookup.

    A model asked "what did South bill in 2025?" has to be able to reach 774 by reading
    rather than by counting. In the flattened text it cannot: the row and the header are
    separated by however many words the extractor emitted between them.
    """
    rows = [line.split("|") for line in to_markdown(GRID).splitlines()]
    header, south = rows[0], rows[-1]

    assert header[3].strip() == "2025"
    assert south[3].strip() == "774"


def test_an_empty_cell_is_empty_rather_than_the_word_none() -> None:
    """pdfplumber returns `None` for a cell it found no text in — a merged span, a blank
    box. Stringified naively that becomes "None", which `to_tsvector` indexes as a word and
    lexical search then retrieves as one."""
    markdown = to_markdown([["Region", "2024"], ["North", None]])

    assert "None" not in markdown
    assert markdown.splitlines()[-1] == "| North |  |"


def test_a_wrapped_cell_stays_on_its_row() -> None:
    """A cell wrapped over two lines arrives with a newline in it, and a literal newline
    inside a Markdown row ends the row — turning one table into two malformed ones."""
    markdown = to_markdown([["Item", "Amount"], ["Total\nrevenue", "12"]])

    assert len(markdown.splitlines()) == 3
    assert "| Total revenue | 12 |" in markdown


def test_a_pipe_in_a_value_does_not_invent_a_column() -> None:
    markdown = to_markdown([["Code", "Note"], ["A|B", "ok"]])

    row = markdown.splitlines()[-1]

    assert "A\\|B" in row
    # Three *unescaped* pipes — two borders and one separator — so the row still has the
    # two columns the header declares.
    assert row.replace("\\|", "").count("|") == 3


def test_a_short_row_is_padded_rather_than_ragged() -> None:
    """A merged cell can make pdfplumber return a row with fewer cells than the header. A
    Markdown table with a ragged row does not render as a table at all."""
    markdown = to_markdown([["A", "B", "C"], ["1"]])

    assert markdown.splitlines()[-1] == "| 1 |  |  |"


def test_a_single_row_is_not_a_table() -> None:
    """A header with no body says nothing a sentence would not. Rendering it as a table
    invents a relationship between values that were only ever next to each other."""
    assert not is_table([["Region", "2024", "2025"]])


def test_a_single_column_is_not_a_table() -> None:
    assert not is_table([["Region"], ["North"], ["South"]])


def test_an_empty_grid_is_not_a_table() -> None:
    """What a ruled page with no text in the boxes returns."""
    assert not is_table([[None, None], [None, None]])


def test_a_real_grid_is_a_table() -> None:
    assert is_table(GRID)


def test_only_the_real_tables_are_rendered() -> None:
    """The page-border case, which is the expensive false positive: rendering it puts a
    header row over data that has no header."""
    border = [["Confidential"]]

    assert rendered([GRID, border]) == [to_markdown(GRID)]


def test_cell_trims_but_keeps_inner_spacing() -> None:
    assert cell("  Total  revenue  ") == "Total  revenue"


def test_the_lexical_index_still_sees_every_value() -> None:
    """Markdown must not cost the lexical half its terms.

    `to_tsvector` drops the pipes as punctuation, so `chunks.tsv` holds the same lexemes it
    held before. Asserted on the string rather than in the database because it is a property
    of what we emit: the values are separated by delimiters, never welded to them.
    """
    markdown = to_markdown(GRID)

    for value in ("Region", "2025", "North", "1,391", "774"):
        assert f" {value} " in markdown
