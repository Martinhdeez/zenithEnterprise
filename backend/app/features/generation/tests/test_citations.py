"""The 0% gate, tested as arithmetic rather than as a hope.

No database and no model here on purpose: these are the rules that decide whether an answer
is allowed to leave the process, and they have to hold for any text any model produces.
"""

from uuid import uuid4

from app.features.generation.answering.citations import bind
from app.features.generation.answering.prompt import ABSTENTION, build
from app.features.retrieval.search import Hit


def hit(text: str = "a passage", page: int = 1) -> Hit:
    return Hit(
        chunk_id=uuid4(),
        document_id=uuid4(),
        filename="contract.pdf",
        page_num=page,
        text=text,
        bboxes=[{"page": 1.0, "x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.2}],
        lexical_rank=1,
        dense_rank=1,
        score=0.03,
    )


def test_a_valid_marker_binds_to_the_passage_it_names() -> None:
    hits = [hit("the first"), hit("the second", page=7)]

    bound = bind("The term is thirty days [2].", hits)

    assert bound.abstained is False
    assert [citation.chunk_id for citation in bound.citations] == [hits[1].chunk_id]
    assert bound.citations[0].page_num == 7
    assert bound.citations[0].bboxes, "a citation has to be clickable, not a page number"


def test_a_marker_naming_a_passage_that_was_never_sent_is_stripped() -> None:
    """The fabrication case, and the only metric in mvp.md whose target is zero.

    A model handed two passages and citing `[9]` is inventing a source. The invention does
    not reach the user, and it does not reach `query_citations` either.
    """
    bound = bind("The penalty is 4% of turnover [9].", [hit(), hit()])

    assert "[9]" not in bound.answer
    assert bound.fabricated == 1


def test_the_valid_half_of_a_half_invented_citation_survives() -> None:
    """`[1, 9]` becomes `[1]`, not nothing.

    Deleting the whole marker would throw away a real reference to a real passage because
    the model got greedy next to it — and would then trip the uncited rule and abstain on an
    answer that was partly grounded.
    """
    hits = [hit("the first"), hit("the second")]

    bound = bind("Both conditions apply [1, 9].", hits)

    assert bound.answer == "Both conditions apply [1]."
    assert [citation.marker for citation in bound.citations] == [1]
    assert bound.fabricated == 1


def test_an_answer_with_no_valid_citation_is_discarded() -> None:
    """The strict rule, and the one that makes the gate measurable.

    An uncited answer may well be correct. There is no way to tell it apart from an invented
    one without reading the corpus — which is the work the user came here to avoid — and if
    it shipped, fabrications would simply stop wearing markers and the gate would measure
    nothing.
    """
    bound = bind("The contract runs for five years.", [hit(), hit()])

    assert bound.abstained is True
    assert bound.answer == ABSTENTION
    assert bound.citations == []


def test_an_answer_whose_only_citation_was_invented_abstains() -> None:
    """Both rules at once: the marker is stripped, and what is left is uncited."""
    bound = bind("The fee is waived [4].", [hit()])

    assert bound.abstained is True
    assert bound.fabricated == 1
    assert "4" not in bound.answer


def test_the_abstention_sentence_is_recognised_and_passed_through() -> None:
    """Recognised by the sentence the model was handed, not by sentiment analysis."""
    bound = bind(f"  {ABSTENTION}  ", [hit(), hit()])

    assert bound.abstained is True
    assert bound.answer == ABSTENTION
    assert bound.fabricated == 0


def test_repeated_markers_produce_one_citation() -> None:
    hits = [hit("the first")]

    bound = bind("It applies [1]. It also applies here [1].", hits)

    assert len(bound.citations) == 1


def test_adjacent_markers_both_bind() -> None:
    """`[1][2]` is what a model writes when a sentence draws on two passages."""
    hits = [hit("the first"), hit("the second")]

    bound = bind("Both say so [1][2].", hits)

    assert [citation.marker for citation in bound.citations] == [1, 2]


def test_stripping_a_marker_does_not_leave_mangled_punctuation() -> None:
    """Cosmetic, and it matters: a visibly broken sentence invites the reader to distrust
    the citation that survived next to it."""
    bound = bind("The clause [1] was amended [7] in 2019 [1].", [hit()])

    assert bound.answer == "The clause [1] was amended in 2019 [1]."


def test_the_prompt_never_shows_the_model_a_chunk_id() -> None:
    """The reason range-checking a marker is sufficient.

    The model can only cite numbers it was given, so a fabricated citation is an integer out
    of range — checkable without touching the database. Put a chunk id in the prompt and a
    fabricated citation becomes a plausible UUID, which could only be caught by a lookup,
    which is a query for a row the model was never shown.
    """
    hits = [hit("the first"), hit("the second")]

    rendered = build("How long is the term?", hits)

    assert str(hits[0].chunk_id) not in rendered
    assert "[1]" in rendered and "[2]" in rendered
    assert "contract.pdf" in rendered, "the model should be able to say where it read it"
