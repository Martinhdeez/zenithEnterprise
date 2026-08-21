"""Tables, rendered as Markdown, because a PDF table extracted as text is not a table.

`extract_text` walks a table the way it walks anything else: line by line, left to right.
A three-column grid comes out as a run of values with the spacing collapsed —

    Region 2024 2025 North 1,204 1,391 South 890 774

— and every relationship in it is gone. Which number belongs to which year is recoverable
only by counting, and neither the embedding model nor the language model counts. Asked
*"what did South bill in 2025?"* over that text, a model has no way to answer except by
guessing, and it guesses confidently.

The same grid as a Markdown table keeps the header attached to its column:

    | Region | 2024  | 2025  |
    | --- | --- | --- |
    | North  | 1,204 | 1,391 |
    | South  | 890   | 774   |

Markdown specifically, rather than HTML or a CSV block: it is what the instruction-tuned
models this product talks to were trained on, it survives chunking as plain text, and it
costs a fraction of the tokens of the equivalent HTML.

**The lexical index gets it for free and this is not incidental.** `to_tsvector` ignores
the pipes as punctuation, so `chunks.tsv` holds exactly the same lexemes it held before —
the cell values are still individually searchable, and hybrid search does not regress on a
page that happens to contain a table.
"""

from collections.abc import Sequence

# A table needs at least a header and a body row, and at least two columns; below that the
# grid pdfplumber found is a layout artefact — a bordered callout, a form field, a page
# frame — and rendering it as a table would state a relationship that does not exist.
MIN_ROWS = 2
MIN_COLUMNS = 2


def cell(value: object) -> str:
    """One cell, flattened to something a Markdown row can hold.

    `None` is pdfplumber's answer for a cell it found no text in — a merged span or an
    empty box — and it must become an empty cell rather than the string "None", which would
    otherwise be indexed as a word and retrieved as one.

    Newlines are the interesting case: a wrapped cell arrives as `"Total\\nrevenue"`, and a
    literal newline inside a Markdown row ends the row. Replaced with a space, which is what
    the cell said. Pipes are escaped for the same reason in reverse — a value containing `|`
    would otherwise invent a column and shift every cell after it.
    """
    text = "" if value is None else str(value)
    return text.replace("\n", " ").replace("|", "\\|").strip()


def is_table(rows: Sequence[Sequence[object]]) -> bool:
    """Whether this grid is worth rendering as one.

    Deliberately strict. A false positive is worse than a false negative here: missing a
    table leaves the page exactly as it is today, while rendering a page border as a table
    puts a header row over data that has no header and teaches the model a relationship
    nobody wrote.
    """
    if len(rows) < MIN_ROWS:
        return False
    if any(len(row) < MIN_COLUMNS for row in rows):
        return False
    # Entirely empty grids come back from ruled pages that contain no text at all.
    return any(cell(value) for row in rows for value in row)


def to_markdown(rows: Sequence[Sequence[object]]) -> str:
    """A pdfplumber table — a list of rows of cells — as a Markdown table.

    The first row becomes the header. That is pdfplumber's convention and usually right;
    when it is wrong the cost is one data row displayed as a header, which is a cosmetic
    error rather than a semantic one, because every value is still in its own column.

    Every row is padded to the widest, since a merged cell can make pdfplumber return a
    short row and a Markdown table with a ragged row renders as something else entirely.
    """
    width = max(len(row) for row in rows)
    padded = [[cell(value) for value in row] + [""] * (width - len(row)) for row in rows]
    header, *body = padded
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def rendered(tables: Sequence[Sequence[Sequence[object]]]) -> list[str]:
    """Every grid on a page that is really a table, as Markdown. Order preserved."""
    return [to_markdown(rows) for rows in tables if is_table(rows)]
