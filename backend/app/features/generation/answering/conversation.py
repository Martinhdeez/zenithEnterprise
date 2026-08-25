"""The thread, and the two limits that keep it from eating the prompt.

A chat that cannot see what was already said is a search box that writes prose: "explain
that more simply" retrieves on those four words and finds nothing, and "what did I ask you
first" is answered by abstaining. Carrying the thread is what makes the difference.

Carrying *all* of it is how a long conversation stops working. Every turn added is prompt
the model reads before it reaches the passages, and the passages are the part that has the
answer in it. So two bounds, both deliberate:

- **Recency.** Only the last few turns. A follow-up refers to what was just said; a message
  referring to something twenty turns back is rare enough that spending the whole budget on
  it every time is the wrong trade.
- **Length.** Answers are truncated, questions are not. A question is a sentence and the
  thing later messages point at; an answer can be a page, and its first lines carry what a
  follow-up is usually about.

Neither bound is a guess about tokens. They are bounds on what is *useful*, which is why
they are stated in turns and characters rather than in a token count that changes with the
tokeniser.
"""

from dataclasses import dataclass

#: Turns kept, counting back from the newest. Six is three exchanges of question and
#: answer — enough for "and what about the other one?" to resolve, short enough that the
#: passages stay the bulk of what the model reads.
MAX_TURNS = 6

#: An answer is cut to its opening. Follow-ups point at what an answer *said*, and an answer
#: says it near the start; the tail is citations and qualifications that no later message
#: refers to.
MAX_ANSWER_CHARACTERS = 600

#: A question is kept whole up to this. Questions are short, and truncating one loses the
#: part a pronoun in the next message is pointing at.
MAX_QUESTION_CHARACTERS = 400


@dataclass(frozen=True, slots=True)
class Turn:
    question: str
    answer: str


@dataclass(frozen=True, slots=True)
class Thread:
    """What was said before this message, already bounded."""

    turns: list[Turn]

    def __bool__(self) -> bool:
        return bool(self.turns)

    def rendered(self) -> str:
        """The thread as the model reads it.

        `User:` and `Assistant:` rather than a JSON array: this text is pasted into a prompt
        for a model that may be an 8B, and mvp.md 5.4 fixed that as the floor to design
        against. A transcript is the one shape every instruction-tuned model has seen.
        """
        return "\n\n".join(
            f"User: {turn.question}\nAssistant: {turn.answer}" for turn in self.turns
        )


def bounded(turns: list[Turn]) -> Thread:
    """The last few turns, each trimmed, oldest first.

    Trimming is marked with an ellipsis rather than done silently. A model handed a sentence
    that stops mid-clause will sometimes complete it as if the speaker had; a visible cut is
    read as a cut.
    """
    kept = turns[-MAX_TURNS:]
    return Thread(
        turns=[
            Turn(
                question=_clip(turn.question, MAX_QUESTION_CHARACTERS),
                answer=_clip(turn.answer, MAX_ANSWER_CHARACTERS),
            )
            for turn in kept
        ]
    )


def _clip(text: str, limit: int) -> str:
    stripped = text.strip()
    return stripped if len(stripped) <= limit else stripped[:limit].rstrip() + "…"
