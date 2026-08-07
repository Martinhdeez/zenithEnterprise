"""The chat behaving like a chat, end to end, against real Postgres and real policies.

Before this, every message was a search: a follow-up retrieved on its own pronouns and
found nothing, and a question about the conversation was answered by telling the user their
documents did not contain the answer. These are the two behaviours that had to change, and
the two guarantees that had to survive the change — no retrieval means no citations, and a
conversational turn is still written to the audit log.
"""

from uuid import UUID

from sqlalchemy import text

from app.core.database import owner_session
from app.features.generation.adapters.mock import MockProvider
from app.features.generation.conversation import Turn
from app.features.generation.service import AnswerService
from app.features.retrieval.service import SearchService
from app.features.retrieval.tests.test_search import profile_for, seed
from conftest import Account, WorkingEmbedder

THREAD = [Turn(question="What must the controller implement?", answer="Technical measures [1].")]


class Watching(SearchService):
    """The real search service, remembering what it was asked to look for.

    Subclassed rather than mocked: the assertion is about which *string* reaches retrieval,
    and everything downstream of that — RLS, the embedder, the ranking — should stay real so
    a passing test still means the answer came out of the corpus.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.queries: list[str] = []

    async def search(self, question: str, limit: int, labels: list[UUID] | None = None):  # type: ignore[override]
        self.queries.append(question)
        return await super().search(question, limit, labels)


async def service(account: Account, model: MockProvider) -> tuple[AnswerService, Watching]:
    profile = await profile_for(account)
    search = Watching(profile, embedder=WorkingEmbedder())
    return AnswerService(profile, provider=model, search=search), search


async def test_a_question_about_the_conversation_is_answered_without_retrieval(
    account: Account,
) -> None:
    """The behaviour the user reported: "what did I ask you first?" is not a search.

    It used to retrieve on those words, find nothing that matched them, and abstain — which
    told the user their documents had failed to answer a question that was never about their
    documents.
    """
    await seed(account.tenant_id, account.default_label)
    # First call is the router, second is the answer.
    model = MockProvider(script=["CHAT", "You asked what the controller must implement."])
    answering, search = await service(account, model)

    result = await answering.answer("what did I ask you first?", history=THREAD)

    assert search.queries == []
    assert not result.abstained
    assert result.citations == []
    assert result.consulted == []
    assert "controller" in result.answer


async def test_a_follow_up_is_retrieved_on_the_resolved_question(account: Account) -> None:
    """ "And the other one?" is not a search query, but what it means is."""
    await seed(account.tenant_id, account.default_label)
    model = MockProvider(
        script=["SEARCH: What must the processor implement?", "The processor must too [1]."]
    )
    answering, search = await service(account, model)

    await answering.answer("and the other one?", history=THREAD)

    assert search.queries == ["What must the processor implement?"]


async def test_a_grounded_answer_still_carries_citations_when_there_is_a_thread(
    account: Account,
) -> None:
    """The thread must not cost the turn its grounding. This is the regression that would
    turn the chat into something that sounds right and cites nothing."""
    await seed(account.tenant_id, account.default_label)
    model = MockProvider(script=["SEARCH: What must the controller implement?", "Measures [1]."])
    answering, _ = await service(account, model)

    result = await answering.answer("and what about that?", history=THREAD)

    assert result.citations
    assert not result.abstained


async def test_the_model_answering_a_grounded_turn_can_see_the_conversation(
    account: Account,
) -> None:
    await seed(account.tenant_id, account.default_label)
    model = MockProvider(script=["SEARCH: What must the controller implement?", "Measures [1]."])
    answering, _ = await service(account, model)

    await answering.answer("and what about that?", history=THREAD)

    _, answering_prompt = model.calls[-1]
    assert "Technical measures [1]." in answering_prompt
    # Still the instruction that keeps it grounded, restated after the transcript.
    assert "must still come from the passages" in answering_prompt


async def test_a_conversational_turn_is_written_to_the_audit_log(account: Account) -> None:
    """A question the user asked that the log does not have is a hole in the record,
    whatever kind of question it was."""
    await seed(account.tenant_id, account.default_label)
    model = MockProvider(script=["CHAT", "You asked about the controller."])
    answering, _ = await service(account, model)

    result = await answering.answer("what did I ask?", history=THREAD)

    async with owner_session() as session:
        stored = await session.scalar(
            text("SELECT question FROM queries WHERE id = :id"), {"id": result.query_id}
        )
    assert stored == "what did I ask?"


async def test_a_conversational_turn_never_carries_a_marker(account: Account) -> None:
    """Rule 2 of `CHAT_SYSTEM` asks the model not to write one; this is what happens when it
    writes one anyway. A `[1]` here would link to a passage that was never consulted."""
    await seed(account.tenant_id, account.default_label)
    model = MockProvider(script=["CHAT", "You asked about the controller [1]."])
    answering, _ = await service(account, model)

    result = await answering.answer("what did I ask?", history=THREAD)

    assert "[1]" not in result.answer
    assert result.citations == []


async def test_the_first_message_of_a_conversation_is_searched_directly(account: Account) -> None:
    """No thread, no router call — the latency of a first question is unchanged."""
    await seed(account.tenant_id, account.default_label)
    model = MockProvider(script=["Measures [1]."])
    answering, search = await service(account, model)

    await answering.answer("What must the controller implement?")

    assert search.queries == ["What must the controller implement?"]
    assert len(model.calls) == 1
