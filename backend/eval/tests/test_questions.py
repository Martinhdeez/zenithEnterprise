"""Verification of the question set, re-derived rather than trusted.

An unverified question set measures the model that drafted the questions, not the retrieval
system. These tests re-resolve every anchor against the extracted text, so a question whose
answer is not where it claims fails here — loudly, at build time — instead of quietly
counting as a retrieval miss and dragging the baseline down for a reason nobody can see.

They need the corpus on disk, so they skip when it is absent rather than turning a fresh
clone red.
"""

import pytest

from eval.corpus import DOCUMENTS, load_manifest
from eval.questions import HEADLINE_TYPES, load_questions

# An answer legitimately restated in a recital and an article is normal; an anchor on more
# than a handful of pages is a theme, not a passage.
MAX_ANCHOR_PAGES = 5

needs_corpus = pytest.mark.skipif(
    not (DOCUMENTS / "gdpr.pdf").exists(),
    reason="corpus not downloaded; run `python -m eval fetch`",
)


def test_there_are_thirty_questions() -> None:
    """Thirty verified beats fifty assumed. The count is pinned because the temptation
    under time pressure is to add unverified ones."""
    assert len(load_questions()) == 30


def test_ids_are_unique() -> None:
    ids = [question.id for question in load_questions()]

    assert len(ids) == len(set(ids))


def test_every_type_is_represented() -> None:
    """Each type measures something different, and a type quietly reaching zero would
    change what the headline means without changing the headline."""
    counts: dict[str, int] = {}
    for question in load_questions():
        counts[question.type] = counts.get(question.type, 0) + 1

    assert counts["factual"] >= 12
    assert counts["cross-document"] >= 5
    assert counts["table"] >= 5
    assert counts["unanswerable"] >= 5


def test_unanswerable_questions_have_no_source() -> None:
    """The definition of the category. One with a source is answerable, and would be
    scored as a fabrication when the system correctly finds it."""
    for question in load_questions():
        if question.type == "unanswerable":
            assert question.sources == (), question.id


def test_answerable_questions_all_have_sources() -> None:
    for question in load_questions():
        if question.type != "unanswerable":
            assert question.sources, question.id


def test_cross_document_questions_really_span_two_documents() -> None:
    """Otherwise they measure ranking, which the factual questions already cover, and the
    fusion half of hybrid retrieval goes untested."""
    for question in load_questions():
        if question.type == "cross-document":
            assert len(set(question.documents)) >= 2, question.id


def test_every_source_document_is_in_the_corpus() -> None:
    known = {document.id for document in load_manifest()}

    for question in load_questions():
        for source in question.sources:
            assert source.document in known, f"{question.id}: unknown document"


@needs_corpus
def test_every_anchor_is_on_every_page_it_claims() -> None:
    """The assertion the whole file exists for.

    If an anchor is not on the page recorded for it, the question is attributing its answer
    to the wrong place — and the measurement would count a correct retrieval as a miss,
    quietly, for the life of the baseline.
    """
    from eval.text import normalise, page_text

    for question in load_questions():
        for source in question.sources:
            for page in source.pages:
                text = normalise(page_text(source.document, page)).lower()
                assert normalise(source.anchor).lower() in text, (
                    f"{question.id}: {source.anchor!r} is not on {source.document} p{page}"
                )


@needs_corpus
def test_recorded_pages_are_exactly_the_pages_that_contain_the_anchor() -> None:
    """Not merely a subset.

    A page missing from the list is a correct retrieval scored as a miss. Re-deriving the
    full set also means the corpus changing under us — a regulator reissuing a PDF — shows
    up here rather than as an unexplained drop in the baseline.
    """
    from eval.text import find

    for question in load_questions():
        for source in question.sources:
            found = tuple(page for _, page in find(source.anchor, [source.document]))
            assert found == source.pages, f"{question.id}: {source.document} moved"


@needs_corpus
def test_anchors_are_distinctive_enough_to_be_evidence() -> None:
    """A phrase appearing across half a document is not evidence that the right passage was
    found — it would score a hit almost wherever retrieval landed.

    **A proportion alone is the wrong rule, and the first version of this test used one.**
    It passed an anchor sitting on 29 of 92 pages, because 32% is under half — while that
    anchor would score a hit almost wherever retrieval landed, quietly inflating the
    baseline. It also failed a perfectly precise anchor on one page of a two-page form.

    So the rule is an absolute cap: at most `MAX_ANCHOR_PAGES` pages, whatever the
    document's length. If an answer genuinely appears in more places than that, the
    question is about a theme rather than a passage, and it is not measuring retrieval.
    """
    for question in load_questions():
        for source in question.sources:
            assert len(source.pages) <= MAX_ANCHOR_PAGES, (
                f"{question.id}: {source.anchor!r} is on {len(source.pages)} pages of "
                f"{source.document} — too broad to be evidence the right passage was found"
            )


def test_the_headline_excludes_tables_and_abstention() -> None:
    """The single most consequential line in the measurement.

    Table questions score near zero because the tracer bullet flattens tables by design,
    and unanswerable questions have no recall at all. Averaging either into the headline
    turns an expected result into a false "retrieval is fundamentally broken".
    """
    assert {"factual", "cross-document"} == HEADLINE_TYPES

    counted = [q for q in load_questions() if q.counts_towards_headline]
    assert len(counted) == 20
    assert all(q.type in {"factual", "cross-document"} for q in counted)
