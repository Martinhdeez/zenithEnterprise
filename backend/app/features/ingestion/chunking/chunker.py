"""Turning pages into the units retrieval actually ranks.

A chunk is what gets embedded, what gets scored, and what gets shown as a citation, so its
size is three trade-offs at once. Too large and the vector averages several topics into
something that matches nothing well; too small and a passage loses the context that made it
answerable. M0 ran its baseline at roughly 1,200 characters with overlap and reached 75%
Recall@8, which is the number this has to at least match.

There is one hard bound rather than a preference: **no chunk may exceed the profile's
`max_batch_tokens`**, or no batch containing it can ever be legal and the document is
permanently unembeddable. That is the shape of the 413 M0 hit, one layer earlier.
"""

from dataclasses import dataclass

from app.features.ingestion.parsers.base import Box, ParsedPage

TARGET_CHARACTERS = 1200
OVERLAP_CHARACTERS = 150
MIN_CHARACTERS = 100

# Characters per token, pessimistically. A real tokeniser would be more accurate and would
# mean loading one here; the cost of overestimating is a smaller batch, and the cost of
# underestimating is a request TEI rejects outright.
CHARACTERS_PER_TOKEN = 3


@dataclass(frozen=True, slots=True)
class Chunk:
    page_num: int
    char_start: int
    char_end: int
    text: str
    boxes: tuple[Box, ...]
    section: str | None = None

    @property
    def estimated_tokens(self) -> int:
        return len(self.text) // CHARACTERS_PER_TOKEN + 1


def chunk_page(page: ParsedPage, target: int = TARGET_CHARACTERS) -> list[Chunk]:
    """Split one page, with overlap, on paragraph then sentence then character boundaries.

    Chunks do not cross pages. That is a deliberate simplification for the MVP: a chunk
    spanning a page break needs boxes on both pages and a citation that highlights two
    places, and the retrieval cost of the occasional split paragraph is smaller than the
    cost of getting that wrong. Recorded in the F5 plan as a known limitation.
    """
    text = page.text
    if len(text.strip()) < MIN_CHARACTERS:
        return []

    chunks: list[Chunk] = []
    start = 0
    while start < len(text):
        end = _boundary(text, start, target)
        body = text[start:end]
        if body.strip():
            chunks.append(
                Chunk(
                    page_num=page.page_num,
                    char_start=start,
                    char_end=end,
                    text=body.strip(),
                    boxes=_boxes_for(page, start, end),
                )
            )
        if end >= len(text):
            break
        # Overlap so a sentence cut in half is whole in one of the two chunks. Without it,
        # a fact spanning the boundary is retrievable from neither.
        start = max(end - OVERLAP_CHARACTERS, start + 1)

    return chunks


def _boundary(text: str, start: int, target: int) -> int:
    """Where to cut, preferring the largest natural break within reach.

    Paragraph, then sentence, then wherever the target lands. Cutting mid-sentence is not
    fatal — the overlap covers it — but a chunk that starts halfway through a clause reads
    badly as a citation, and citations are shown to people.
    """
    end = min(start + target, len(text))
    if end == len(text):
        return end

    window = text[start:end]
    for separator in ("\n\n", ". ", "\n"):
        cut = window.rfind(separator)
        # Only accept a break in the last third: an early one produces a chunk far below
        # target and multiplies the number of chunks for the whole corpus.
        if cut > target * 0.6:
            return start + cut + len(separator)
    return end


def _boxes_for(page: ParsedPage, start: int, end: int) -> tuple[Box, ...]:
    """The rectangles covering this slice of text.

    Matched by walking the words in order and consuming the same characters the text does.
    It is approximate where the extractor inserted whitespace the word list does not
    contain, which is why the result is a set of line boxes rather than a claim about exact
    glyph positions — the viewer highlights lines, not characters.
    """
    if not page.words:
        return ()

    boxes: list[Box] = []
    cursor = 0
    for word in page.words:
        found = page.text.find(word.text, cursor)
        if found == -1:
            continue
        cursor = found + len(word.text)
        if found >= end:
            break
        if cursor > start:
            boxes.append(word.box)

    return tuple(_merge_lines(boxes))


def _merge_lines(boxes: list[Box]) -> list[Box]:
    """Word boxes collapsed into line boxes.

    A 1,200-character chunk is a few hundred words, and storing a rectangle per word would
    put hundreds of objects in a JSONB column for every chunk in the corpus. Words on the
    same line are merged into one box, which is also what a highlight should look like.
    """
    if not boxes:
        return []

    merged: list[Box] = []
    for box in sorted(boxes, key=lambda item: (item.page, round(item.y0, 3), item.x0)):
        last = merged[-1] if merged else None
        same_line = last is not None and last.page == box.page and abs(last.y0 - box.y0) < 0.005
        if last is not None and same_line:
            merged[-1] = Box(
                page=last.page,
                x0=min(last.x0, box.x0),
                y0=min(last.y0, box.y0),
                x1=max(last.x1, box.x1),
                y1=max(last.y1, box.y1),
            )
        else:
            merged.append(box)
    return merged
