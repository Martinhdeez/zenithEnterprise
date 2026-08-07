"""Bounding the thread, which is what stops a long conversation from crowding out the
passages that actually answer the question."""

from app.features.generation import conversation
from app.features.generation.conversation import Turn, bounded


def turns(count: int) -> list[Turn]:
    return [Turn(question=f"question {i}", answer=f"answer {i}") for i in range(count)]


def test_a_short_thread_is_kept_whole() -> None:
    assert len(bounded(turns(3)).turns) == 3


def test_only_the_most_recent_turns_survive() -> None:
    """Counting back from the newest, because a follow-up refers to what was just said."""
    kept = bounded(turns(20)).turns

    assert len(kept) == conversation.MAX_TURNS
    assert kept[-1].question == "question 19"
    assert kept[0].question == f"question {20 - conversation.MAX_TURNS}"


def test_a_long_answer_is_cut_and_shows_that_it_was() -> None:
    """Visibly, not silently. A model handed a sentence that stops mid-clause sometimes
    finishes it as though the speaker had."""
    trimmed = bounded([Turn(question="q", answer="x" * 5_000)]).turns[0]

    assert len(trimmed.answer) <= conversation.MAX_ANSWER_CHARACTERS + 1
    assert trimmed.answer.endswith("…")


def test_a_real_question_is_never_the_thing_that_gets_cut() -> None:
    """Questions are bounded too, but the bound is far above any question a person types.

    They are kept whole because a pronoun in the next message points at what the last
    question *said*, and a question clipped mid-clause is the one place the thread stops
    being resolvable.
    """
    question = "What are the penalties under GDPR for a repeated infringement, and how do " * 4
    assert len(question) < conversation.MAX_QUESTION_CHARACTERS

    assert bounded([Turn(question=question, answer="short")]).turns[0].question == question.strip()


def test_an_empty_thread_is_falsey() -> None:
    """`routing.resolve` skips its model call on this, so it is load-bearing rather than
    convenience."""
    assert not bounded([])
    assert bounded(turns(1))


def test_the_transcript_names_both_speakers() -> None:
    rendered = bounded([Turn(question="What is VAT?", answer="A tax [1].")]).rendered()

    assert "User: What is VAT?" in rendered
    assert "Assistant: A tax [1]." in rendered
