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


#: How far apart two boxes on the same line may be before they stop being one line.
#:
#: A column gutter on an A4 page is roughly 4–6% of the width; the space between words is
#: well under 1%, and the gap left by a tab or a right-aligned page number sits between the
#: two. Three percent separates them with room on both sides. It is deliberately not
#: derived from the gutter positions `columns.py` finds: those describe the page a box came
#: from, this describes two boxes, and threading page geometry down here would couple the
#: chunker to the parser for a number that does not need it.
MAX_LINE_GAP = 0.03


def _merge_lines(boxes: list[Box]) -> list[Box]:
    """Word boxes collapsed into line boxes.

    A 1,200-character chunk is a few hundred words, and storing a rectangle per word would
    put hundreds of objects in a JSONB column for every chunk in the corpus. Words on the
    same line are merged into one box, which is also what a highlight should look like.

    **Vertical alignment alone is not enough to call two boxes one line.** On a two-column
    page the last word of the left column and the first of the right sit at the same `y0` —
    physically true, and not one line of anything. Merging on that produced highlights
    spanning the full page width: 19% of the boxes on `bert-paper.pdf` covered more than
    three quarters of the page, the widest running 0.121 → 0.883. So proximity is required
    as well as alignment, and the gutter is exactly the gap that fails it.
    """
    if not boxes:
        return []

    merged: list[Box] = []
    for box in sorted(boxes, key=lambda item: (item.page, round(item.y0, 3), item.x0)):
        last = merged[-1] if merged else None
        same_line = (
            last is not None
            and last.page == box.page
            and abs(last.y0 - box.y0) < 0.005
            # Sorted by `x0`, so `last` is always the box to the left and the gap is the
            # distance from where it ends to where this one starts. Negative when they
            # overlap, which is common and still one line.
            and box.x0 - last.x1 < MAX_LINE_GAP
        )
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
