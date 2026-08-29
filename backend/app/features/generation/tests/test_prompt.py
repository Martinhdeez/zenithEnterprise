"""The prompt rules, each tied to the measured failure that produced it.

Every rule here was written after watching Llama 3.1 8B get a question wrong with the right
passage in front of it (F9 results §4). Without these tests the rules are folklore, and the
first person to tidy the prompt deletes the one sentence that was load-bearing.

They assert the *instructions*, not the model's compliance. Whether an 8B model obeys rule
7 is a question for `python -m eval answers`, which measures it against 36 questions; what
a unit test can guarantee is that the instruction is still there to obey.
"""

from uuid import uuid4

from app.features.generation.answering.prompt import ABSTENTION, SYSTEM, build
from app.features.retrieval.search import Hit


def hit(filename: str = "irs-pub-15.pdf", page: int = 15, text: str = "the passage") -> Hit:
    return Hit(
        chunk_id=uuid4(),
        document_id=uuid4(),
        filename=filename,
        media_type="application/pdf",
        page_num=page,
        char_start=0,
        char_end=len(text),
        text=text,
        bboxes=[],
        lexical_rank=1,
        dense_rank=1,
        score=0.03,
    )


def test_the_filename_precedes_the_passage_rather_than_trailing_the_marker() -> None:
    """The measured failure: "the document is [3] (from irs-pub-15, page 15)".

    Asked which document stated the withholding rates, the model answered with the citation
    marker — because the marker came first and the filename read as an aside. A citation
    marker is not a name, and the layout has to say so before the rules have to.
    """
    rendered = build("which document?", [hit()])

    assert rendered.index("irs-pub-15.pdf") < rendered.index("the passage")
    assert "[1] irs-pub-15.pdf" in rendered


def test_passage_text_is_delimited() -> None:
    """The measured failure: an answer beginning "Our work aims to expand the space...".

    That is the RAG paper's own first-person prose, copied out. With no visible boundary
    around the passage the model treats it as its own voice, and the answer becomes a
    quotation attributed to nobody.
    """
    rendered = build("what does it do?", [hit(text="Our work aims to expand the space")])

    assert '"""\nOur work aims to expand the space\n"""' in rendered


def test_the_rules_forbid_reciting_and_copying() -> None:
    """Rules 6 and 7, both measured.

    "According to passage [4], the term State has the meaning given..." is an answer that
    describes where a fact lives instead of stating it.
    """
    assert "According to passage" in SYSTEM
    assert '"we"' in SYSTEM and '"our"' in SYSTEM


def test_a_bare_yes_is_forbidden() -> None:
    """A regression the first version of these rules caused, and the reason they are tested.

    Rules 6 and 9 together ("start with the answer", "be brief") compressed a substantive
    cross-document answer down to `Yes [1][2][3][5].` — correct, cited, and useless. Brevity
    has to be bounded or it eats the answer.
    """
    assert 'never answer with only "yes" or "no"' in SYSTEM


def test_naming_a_document_excludes_the_marker_and_the_page() -> None:
    """The second regression, and the same failure wearing a new costume.

    Moving the filename ahead of the passage stopped the model calling a document `[3]` —
    and started it answering `the paper is [5] attention-is-all-you-need — page 1`, copying
    the header line whole. The layout alone cannot fix this; the rule has to say which part
    of that line is the name.
    """
    assert "not the marker, not the page number" in SYSTEM


def test_the_rules_require_every_part_of_a_question_answered() -> None:
    """Three of the six real F9 failures were two-part questions answered halfway —
    "which document states the withholding rates, and which explains the return?" got a
    confident answer to the first half and "not explicitly stated" for the second."""
    assert "If it asks for two things, answer both." in SYSTEM


def test_the_abstention_sentence_is_quoted_verbatim_in_the_rules() -> None:
    """`citations.bind` recognises this exact sentence. If the prompt and the binder ever
    disagree by a word, a correct abstention stops being recognised as one and is instead
    discarded as an uncited answer — the same outcome, arrived at by accident, and with the
    `abstained` flag no longer meaning what it says."""
    assert ABSTENTION in SYSTEM


def test_the_prompt_still_shows_no_chunk_ids() -> None:
    """Unchanged by the rewrite, and the reason range-checking a marker is sufficient: the
    model can only cite numbers it was given."""
    passage = hit()

    rendered = build("anything?", [passage])

    assert str(passage.chunk_id) not in rendered
    assert str(passage.document_id) not in rendered
