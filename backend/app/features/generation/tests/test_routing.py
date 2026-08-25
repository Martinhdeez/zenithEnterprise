"""Routing a message against the thread, and the promise that it cannot make things worse.

The router exists because two kinds of message were being handled as one. "Explain that
more simply" retrieved on those four words; "what did I ask you first" was answered by
telling the user their documents do not contain the answer. Neither is a search.

The assertions that matter most here are the failure ones. This step sits in front of
retrieval on every turn of a conversation, so a router that breaks takes search with it —
unless every path it can fail down lands on searching the message as written, which is what
the pipeline did before it existed.
"""

from dataclasses import dataclass

import pytest

from app.common.llm import BaseLLMProvider, GenerationResponse
from app.features.generation.answering import routing
from app.features.generation.answering.conversation import Thread, Turn


@dataclass
class Replying(BaseLLMProvider):
    """A provider that answers with whatever the test hands it, and remembers the prompt."""

    reply: str
    name = "replying"
    seen: str = ""

    async def complete(self, system: str, user: str) -> GenerationResponse:
        self.seen = user
        return GenerationResponse(text=self.reply, model="test")


class Broken(BaseLLMProvider):
    name = "broken"

    async def complete(self, system: str, user: str) -> GenerationResponse:
        raise RuntimeError("the model is unreachable")


THREAD = Thread(
    turns=[Turn(question="What are the penalties under GDPR?", answer="Fines up to 4% [1].")]
)


async def test_a_first_message_is_searched_without_asking_the_model() -> None:
    """No thread, nothing to resolve against — and no round trip spent finding that out.

    This is the common case. A router that added a model call to every first question would
    be a latency regression on the path most questions take.
    """
    provider = Replying(reply="CHAT")

    intent = await routing.resolve(Thread(turns=[]), "What is the VAT rate?", provider)

    assert intent.query == "What is the VAT rate?"
    assert provider.seen == ""


async def test_a_follow_up_is_rewritten_to_stand_on_its_own() -> None:
    """The point of the whole file: "and the other one?" is not a search query."""
    provider = Replying(reply="SEARCH: What are the penalties under the Data Act?")

    intent = await routing.resolve(THREAD, "and the other one?", provider)

    assert intent.query == "What are the penalties under the Data Act?"
    assert not intent.conversational


async def test_a_question_about_the_conversation_is_not_a_search() -> None:
    provider = Replying(reply="CHAT")

    intent = await routing.resolve(THREAD, "what did I ask you first?", provider)

    assert intent.conversational
    assert intent.query is None


async def test_the_thread_is_what_the_router_reads() -> None:
    provider = Replying(reply="CHAT")

    await routing.resolve(THREAD, "and that?", provider)

    assert "What are the penalties under GDPR?" in provider.seen
    assert "and that?" in provider.seen


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "I think the user wants to know about penalties.",
        "Sure! Here is the rewritten query:",
        "SEARCH:",
        "SEARCH:   ",
    ],
)
async def test_a_reply_that_is_not_one_of_the_two_shapes_searches_the_message(reply: str) -> None:
    """A small model asked for one line will sometimes answer the question instead.

    Every one of these used to be a plausible way to end up searching for nothing. They all
    end up searching for exactly what the user typed, which is the behaviour that shipped
    before the router existed.
    """
    intent = await routing.resolve(THREAD, "what about VAT?", Replying(reply=reply))

    assert intent.query == "what about VAT?"


async def test_an_unreachable_model_does_not_take_search_down_with_it() -> None:
    """The assertion this design lives on.

    The router calls a model, and models go down. If that turned into an exception the chat
    would stop answering questions it could have answered — with retrieval and generation
    both healthy — because the *routing* step failed.
    """
    intent = await routing.resolve(THREAD, "what about VAT?", Broken())

    assert intent.query == "what about VAT?"
    assert not intent.conversational


async def test_a_quoted_rewrite_is_unwrapped() -> None:
    intent = await routing.resolve(THREAD, "and that?", Replying(reply='SEARCH: "GDPR fines"'))

    assert intent.query == "GDPR fines"


async def test_extra_lines_after_the_verdict_are_ignored() -> None:
    provider = Replying(reply="CHAT\n\nI chose this because the user is asking about the thread.")

    assert (await routing.resolve(THREAD, "what did I ask?", provider)).conversational
