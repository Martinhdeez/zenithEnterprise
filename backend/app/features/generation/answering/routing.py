"""What kind of message is this, and what should be searched for it.

Two failures come from treating every message as a search. "Explain that more simply"
retrieves on those four words and finds nothing that matches them, so a follow-up about an
answer already given is answered by abstaining. And "what did I ask you first?" is not a
question about the corpus at all — there is no passage that answers it and there should not
be, but the only vocabulary the pipeline had was retrieve-then-abstain.

So one step before retrieval, resolving the message against the thread into one of two
things: a self-contained search query, or the statement that this turn is about the
conversation itself.

**It cannot break search.** A router that misfires and returns nothing usable falls back to
the raw message, which is exactly what the pipeline did before this file existed. Every
error path here — an unreachable model, a timeout, a reply in an unexpected shape — lands
on that same fallback. The worst outcome of the router being wrong is the behaviour we
already shipped; the best is a follow-up that works.

**It costs nothing on the first message.** An empty thread has nothing to resolve against,
so no call is made. That is the common case, and it stays at the latency it had.
"""

from __future__ import annotations

import structlog

from app.common.llm import BaseLLMProvider
from app.features.generation.conversation import Thread

log = structlog.get_logger()

#: The literal the model writes for a turn that needs no retrieval. Matched exactly, on a
#: token the model was handed rather than a phrase it invented — the same contract
#: `prompt.ABSTENTION` uses, for the same reason.
CHAT = "CHAT"
SEARCH_PREFIX = "SEARCH:"

SYSTEM = f"""You prepare a user's message for a document search system.

Read the conversation, then the new message, and reply with exactly one line.

If the new message can be answered using only the conversation above — it asks what was
said, what the user asked before, or asks you to rephrase, shorten, translate or explain
your own previous answer, or it is a greeting or small talk — reply with exactly:
{CHAT}

Otherwise the message needs the documents. Reply with:
{SEARCH_PREFIX} <the message rewritten so it makes sense on its own>

When rewriting, replace words like "it", "that", "the second one" with what they refer to
in the conversation. Keep the user's wording everywhere else. If the message already makes
sense on its own, repeat it unchanged.

Reply with one line and nothing else. Never explain your choice."""


class Intent:
    """One of two outcomes. A class rather than an enum plus a payload because the payload
    only exists for one of them, and a `query` that is `None` half the time is a bug
    waiting to be written."""

    __slots__ = ("query",)

    def __init__(self, query: str | None) -> None:
        #: The standalone search query, or `None` when this turn is conversational.
        self.query = query

    @property
    def conversational(self) -> bool:
        return self.query is None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Intent(query={self.query!r})"


def grounded(query: str) -> Intent:
    return Intent(query)


CONVERSATIONAL = Intent(None)


async def resolve(thread: Thread, message: str, provider: BaseLLMProvider) -> Intent:
    """Route the message, or fall back to searching it as written.

    The fallback is not an error path bolted on: it is the definition of what this function
    does when it cannot do better. Nothing here raises.
    """
    if not thread:
        # Nothing to resolve against, and nothing a first message could be a follow-up to.
        return grounded(message)

    prompt = f"Conversation:\n\n{thread.rendered()}\n\nNew message: {message}\n\nReply:"
    try:
        reply = (await provider.complete(SYSTEM, prompt)).text.strip()
    except Exception as error:
        # Includes the model being unreachable. A chat that stops working because the
        # *router* is down would be a worse product than one that never had a router.
        log.warning("routing_failed", error=str(error))
        return grounded(message)

    return _read(reply, message)


def _read(reply: str, message: str) -> Intent:
    """Parse the one line, and distrust it.

    A small model asked for one line will sometimes write three, or wrap the line in quotes,
    or answer the question instead of routing it. Only two shapes are accepted; everything
    else searches the message as written.
    """
    first = reply.splitlines()[0].strip() if reply else ""

    if first.upper().startswith(CHAT):
        return CONVERSATIONAL

    if first.upper().startswith(SEARCH_PREFIX):
        query = first[len(SEARCH_PREFIX) :].strip().strip('"').strip()
        # A rewrite that came back empty is not a rewrite. Falling back to the message keeps
        # the turn working; searching for "" would retrieve nothing and abstain.
        return grounded(query or message)

    log.warning("routing_unparsed", reply=first[:120])
    return grounded(message)
