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
from app.features.generation.answering.conversation import Turn
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
        #: The `documents` each call was scoped to — `None` for an ordinary question.
        self.scopes: list[list[UUID] | None] = []

    async def search(  # type: ignore[override]
        self,
        question: str,
        limit: int,
        labels: list[UUID] | None = None,
        documents: list[UUID] | None = None,
    ):
        self.queries.append(question)
        self.scopes.append(documents)
        return await super().search(question, limit, labels, documents)


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


async def test_a_scoped_question_is_answered_only_from_the_named_document(
    account: Account,
) -> None:
    """The `@` mention, end to end: the model never sees the other document.

    Asserted on the passages that reached the prompt rather than on the prose that came
    back, because a mock provider's answer proves nothing about what it was given. The
    context is where the guarantee lives — a fact the model was never shown is one it cannot
    repeat.
    """
    handbook = await seed(
        account.tenant_id,
        account.default_label,
        [("The severance allowance is thirty days of salary per year.", 1)],
    )
    await seed(
        account.tenant_id,
        account.default_label,
        [("The severance allowance is twenty days of salary per year.", 1)],
    )
    model = MockProvider(script=["SEARCH severance allowance", "Thirty days [1]."])
    answering, search = await service(account, model)

    answer = await answering.answer("severance allowance", documents=[handbook])

    assert search.scopes[-1] == [handbook]
    assert {hit.document_id for hit in answer.consulted} == {handbook}
    assert all("twenty" not in hit.text for hit in answer.consulted)


async def test_naming_a_document_forces_retrieval_on_a_conversational_turn(
    account: Account,
) -> None:
    """ "And what about this one?" with a document attached is not a conversational turn.

    The router is right to classify those words as chat — in a thread they usually are — but
    a caller who attached `@handbook.pdf` has said explicitly which document they mean, and
    answering from the thread alone would ignore the only unambiguous thing in the request.
    """
    handbook = await seed(account.tenant_id, account.default_label)
    model = MockProvider(script=["CHAT", "Technical measures [1]."])
    answering, search = await service(account, model)

    await answering.answer("and what about this one?", history=THREAD, documents=[handbook])

    assert search.queries, "a named document must send the turn to retrieval"
    assert search.scopes[-1] == [handbook]


async def test_an_unscoped_question_still_reads_the_whole_corpus(account: Account) -> None:
    """The regression guard: mentions are opt-in, and the default is unchanged."""
    await seed(account.tenant_id, account.default_label)
    await seed(account.tenant_id, account.default_label, [("A second document's passage.", 3)])
    model = MockProvider(script=["SEARCH controller measures", "Technical measures [1]."])
    answering, search = await service(account, model)

    answer = await answering.answer("what must the controller implement?")

    assert search.scopes[-1] is None
    assert len({hit.document_id for hit in answer.consulted}) == 2
