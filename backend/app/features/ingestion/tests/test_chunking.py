"""Chunking, and the two properties that are load-bearing downstream.

A chunk that exceeds the token budget can never be embedded, and a chunk with no boxes can
never be highlighted — and boxes exist only during parsing, so the second failure is not
repairable without re-parsing the whole corpus.
"""

import pytest

from app.core.hardware import PROFILES
from app.features.ingestion.chunking.chunker import (
    OVERLAP_CHARACTERS,
    TARGET_CHARACTERS,
    _merge_lines,  # type: ignore[reportPrivateUsage]
    chunk_page,
    chunk_stream,
)
from app.features.ingestion.parsers.base import Box, ParsedPage, Word

SENTENCE = "The controller shall implement appropriate technical and organisational measures. "


WORDS_PER_LINE = 10


def page_of(text: str, with_words: bool = True) -> ParsedPage:
    """A page laid out the way a real one is: several words share a line.

    That detail is the whole point of the merging test — a fixture that gives every word
    its own vertical position cannot show line merging, and would pass whatever the code
    did.
    """
    words: list[Word] = []
    if with_words:
        for index, token in enumerate(text.split()):
            line = index // WORDS_PER_LINE
            column = index % WORDS_PER_LINE
            top = round((line % 90) * 0.01, 4)
            words.append(
                Word(
                    token,
                    Box(
                        1,
                        round(0.05 + column * 0.09, 4),
                        top,
                        round(0.05 + column * 0.09 + 0.08, 4),
                        round(top + 0.008, 4),
                    ),
                )
            )
    return ParsedPage(page_num=1, text=text, words=tuple(words))


def test_a_short_page_produces_nothing() -> None:
    """A page of a dozen characters is a page number or a header. Chunking it puts noise
    into the index that competes with real passages for a place in the top eight."""
    assert chunk_page(page_of("Page 12")) == []


def test_chunks_cover_the_page_and_overlap() -> None:
    """Overlap is not redundancy. A fact split across the boundary is retrievable from
    neither chunk without it."""
    text = SENTENCE * 60
    chunks = chunk_page(page_of(text))

    assert len(chunks) > 1
    for previous, following in zip(chunks, chunks[1:], strict=False):
        assert following.char_start < previous.char_end, "chunks do not overlap"
        assert previous.char_end - following.char_start <= OVERLAP_CHARACTERS + 1


def test_no_chunk_can_exceed_any_profile_token_budget() -> None:
    """The 413 M0 hit, prevented one layer earlier.

    A chunk larger than `max_batch_tokens` makes every batch containing it illegal, and the
    document is then permanently unembeddable — a failure that looks like an infrastructure
    problem and is actually a chunking one.
    """
    chunks = chunk_page(page_of(SENTENCE * 100))
    smallest = min(profile.max_batch_tokens for profile in PROFILES.values())

    assert chunks
    for chunk in chunks:
        assert chunk.estimated_tokens < smallest


def test_chunks_prefer_a_sentence_boundary() -> None:
    """Citations are shown to people. A chunk beginning halfway through a clause reads as a
    bug even when the retrieval was correct."""
    chunks = chunk_page(page_of(SENTENCE * 40))

    assert chunks[0].text.endswith(".")


def test_every_chunk_carries_boxes() -> None:
    """Boxes exist only during parsing. Omitting them forces a re-parse of the corpus rather
    than a re-embed — M0 priced that at eight hours for ten thousand pages."""
    chunks = chunk_page(page_of(SENTENCE * 30))

    assert all(chunk.boxes for chunk in chunks)
    assert all(
        0.0 <= box.x0 <= 1.0 and 0.0 <= box.y1 <= 1.0 for chunk in chunks for box in chunk.boxes
    )


def test_boxes_are_merged_into_lines_not_words() -> None:
    """A rectangle per word would put hundreds of objects in a JSONB column for every chunk
    in the corpus, and a per-word highlight is not what a reader wants to see anyway."""
    text = SENTENCE * 30
    chunks = chunk_page(page_of(text))

    words_in_first = len(chunks[0].text.split())
    assert len(chunks[0].boxes) < words_in_first


def test_a_page_without_word_positions_still_chunks() -> None:
    """Docling may return text without the same word structure, and a page that cannot be
    highlighted is still a page that should be searchable."""
    chunks = chunk_page(page_of(SENTENCE * 20, with_words=False))

    assert chunks
    assert all(chunk.boxes == () for chunk in chunks)


def test_the_target_size_is_what_m0_measured_at() -> None:
    """75% Recall@8 was measured at roughly this size. Changing it silently would move the
    baseline the reranker is supposed to beat, and nobody would know why."""
    assert TARGET_CHARACTERS == 1200


# --- Column gutters ------------------------------------------------------------------
#
# `_merge_lines` collapses word boxes into line boxes so a chunk stores a handful of
# rectangles rather than hundreds. It used to decide "same line" from vertical alignment
# alone, which is true of the last word of a left column and the first of a right one —
# physically true, and not one line of anything.


def _box(x0: float, x1: float, y0: float = 0.5, page: int = 1) -> Box:
    return Box(page=page, x0=x0, y0=y0, x1=x1, y1=y0 + 0.01)


def test_boxes_across_a_gutter_stay_separate() -> None:
    """The defect, at its smallest: two boxes level with each other, a column apart.

    Merged, this is a highlight spanning the whole page width for a paragraph that occupies
    half of it.
    """
    left = _box(0.10, 0.45)
    right = _box(0.55, 0.90)

    merged = _merge_lines([left, right])

    assert len(merged) == 2, "a gutter is not a word space"
    assert merged[0].x1 <= 0.45 and merged[1].x0 >= 0.55


def test_words_on_one_line_still_merge() -> None:
    """The behaviour that has to survive the guard: ordinary adjacent words are one line,
    and storing a rectangle per word would put hundreds of objects in JSONB per chunk."""
    merged = _merge_lines([_box(0.10, 0.20), _box(0.205, 0.31), _box(0.315, 0.42)])

    assert len(merged) == 1
    assert merged[0].x0 == pytest.approx(0.10)
    assert merged[0].x1 == pytest.approx(0.42)


def test_overlapping_boxes_are_one_line() -> None:
    """Kerned or overlapping glyph boxes produce a negative gap, which is common and still
    one line — the guard must compare against the gap, not its absolute value."""
    merged = _merge_lines([_box(0.10, 0.25), _box(0.24, 0.40)])

    assert len(merged) == 1


def test_boxes_on_different_lines_stay_separate() -> None:
    """Proximity is required *as well as* alignment, not instead of it."""
    merged = _merge_lines([_box(0.10, 0.20, y0=0.30), _box(0.205, 0.31, y0=0.60)])

    assert len(merged) == 2


# --- documents with no pages -------------------------------------------------------------
#
# `chunk_stream` is what a `.txt` or `.md` goes through. Its two load-bearing properties are
# different from `chunk_page`'s: there are no boxes to check, and the offsets are the
# citation rather than a debugging aid, so they have to be exactly right.


def test_a_text_document_has_no_page_numbers() -> None:
    """`None`, not 1. A placeholder is what puts "page 1" under a Markdown file."""
    chunks = chunk_stream(SENTENCE * 40)

    assert chunks
    assert all(chunk.page_num is None for chunk in chunks)


def test_a_text_document_has_no_boxes() -> None:
    """There is no geometry to have. An empty tuple, never an invented rectangle."""
    assert all(chunk.boxes == () for chunk in chunk_stream(SENTENCE * 40))


def test_offsets_index_the_text_they_came_from() -> None:
    """The property the whole feature rests on.

    A text citation is underlined by slicing the file at `char_start:char_end`. If those
    offsets do not select the chunk's own text, every highlight in the product is in the
    wrong place — and it would look plausible, because it would still be somewhere in the
    document.
    """
    body = SENTENCE * 40

    for chunk in chunk_stream(body):
        assert body[chunk.char_start : chunk.char_end].strip() == chunk.text


def test_offsets_run_the_length_of_the_document_not_of_a_page() -> None:
    """`chunk_page` restarts at zero on each page; this must not.

    A reader that assumed one rule for both would highlight the wrong span, and the two
    functions exist separately so that assumption cannot be made silently.
    """
    body = SENTENCE * 60
    chunks = chunk_stream(body)

    assert chunks[0].char_start == 0
    assert chunks[-1].char_end == len(body)
    assert chunks[-1].char_start > TARGET_CHARACTERS


def test_consecutive_chunks_overlap_so_a_split_sentence_survives_whole() -> None:
    """Same guarantee `chunk_page` gives, and for the same reason: a fact spanning the
    boundary must be retrievable from at least one chunk rather than from neither."""
    chunks = chunk_stream(SENTENCE * 60)

    assert len(chunks) > 1
    for earlier, later in zip(chunks, chunks[1:], strict=False):
        assert later.char_start < earlier.char_end


def test_a_document_too_short_to_be_worth_indexing_produces_nothing() -> None:
    """Same floor as a page. A two-line note is not a searchable document, and admitting
    one would put a chunk in the index that matches everything weakly."""
    assert chunk_stream("Restart it.") == []


def test_no_text_chunk_can_exceed_the_token_budget() -> None:
    """The one hard bound, and it applies to both splitters.

    A chunk over the budget is permanently unembeddable — no batch containing it is ever
    legal — so the document would ingest and then fail at the last stage. Measured against
    the smallest budget any profile sets, the same way `chunk_page`'s bound is: a chunk
    that fits everywhere fits here.
    """
    smallest = min(profile.max_batch_tokens for profile in PROFILES.values())

    assert all(chunk.estimated_tokens < smallest for chunk in chunk_stream(SENTENCE * 200))
