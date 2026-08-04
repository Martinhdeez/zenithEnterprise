"""Generation end to end, against real Postgres and real policies.

The assertions that matter are the ones where a mistake means a customer reads a citation
pointing at a document they cannot open, or an answer nobody can explain six months later.
"""

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.common.llm import GenerationUnavailableError
from app.core.database import owner_session
from app.features.auth.permissions import CATALOGUE
from app.features.auth.service import AccessProfile
from app.features.generation.adapters.mock import MockProvider
from app.features.generation.prompt import ABSTENTION
from app.features.generation.service import AnswerService
from app.features.retrieval.service import SearchService
from app.features.retrieval.tests.test_search import profile_for, seed
from app.features.tenancy.context import TenantContext
from conftest import Account, WorkingEmbedder

QUESTION = "What must the controller implement?"


# `MockProvider` rather than a stub defined here, deliberately: it is the shipped second
# implementation of `BaseLLMProvider`, so every test in this file is also a check that the
# abstraction has more than one member. A local stub would let the interface drift and the
# drift would only surface when a real second provider was written.


async def answering(account: Account, model: MockProvider, labels: list[UUID] | None = None):
    profile = await profile_for(account)
    search = SearchService(profile, embedder=WorkingEmbedder())  # type: ignore[arg-type]
    return await AnswerService(profile, provider=model, search=search).answer(QUESTION, labels)


@dataclass(frozen=True, slots=True)
class StoredCitation:
    chunk_id: UUID
    rank: int
    bm25: float | None
    vector: float | None
    rrf: float | None
    rerank: float | None


async def citations_of(query_id: UUID) -> list[StoredCitation]:
    """Read back through the owner, so the test sees what was really written rather than
    what the writing code believes it wrote."""
    async with owner_session() as session:
        rows = await session.execute(
            text(
                "SELECT chunk_id, rank, score_bm25, score_vector, score_rrf, score_rerank "
                "FROM query_citations WHERE query_id = :q ORDER BY rank"
            ),
            {"q": query_id},
        )
        return [
            StoredCitation(
                chunk_id=row.chunk_id,
                rank=row.rank,
                bm25=row.score_bm25,
                vector=row.score_vector,
                rrf=row.score_rrf,
                rerank=row.score_rerank,
            )
            for row in rows
        ]


async def test_an_answer_cites_a_passage_that_was_actually_retrieved(account: Account) -> None:
    await seed(account.tenant_id, account.default_label)

    result = await answering(account, MockProvider(["Technical measures for data protection [1]."]))

    assert result.abstained is False
    assert len(result.citations) == 1
    assert result.citations[0].chunk_id in {hit.chunk_id for hit in result.consulted}
    assert result.citations[0].bboxes, "clicking a citation has to open the page"


async def test_the_four_score_columns_are_populated_with_real_numbers(account: Account) -> None:
    """`query_citations` was designed with four score columns so that six months from now
    someone can answer *why did this query return garbage*. F6 and F7 filled none of them."""
    await seed(account.tenant_id, account.default_label)

    result = await answering(account, MockProvider(["Measures [1]."]))
    stored = await citations_of(result.query_id)

    assert len(stored) == 1
    row = stored[0]
    assert row.chunk_id == result.citations[0].chunk_id
    assert row.rank >= 1
    assert row.rrf is not None and row.rrf > 0
    assert row.bm25 is not None, "ts_rank_cd, under the column's legacy name"
    assert row.vector is not None, "cosine similarity, bigger is better"


async def test_only_the_cited_passages_are_logged(account: Account) -> None:
    """Eight passages are consulted and one is cited. Logging all eight would make the table
    describe the shortlist rather than the answer."""
    await seed(account.tenant_id, account.default_label)

    result = await answering(account, MockProvider(["Only the first one matters [1]."]))
    stored = await citations_of(result.query_id)

    assert len(result.consulted) > 1
    assert len(stored) == 1


async def test_a_fabricated_citation_never_reaches_the_answer_or_the_log(
    account: Account,
) -> None:
    """The 0% gate, at the level where it is visible to a customer."""
    await seed(account.tenant_id, account.default_label)

    result = await answering(account, MockProvider(["The penalty is 4% of turnover [99]."]))
    stored = await citations_of(result.query_id)

    assert "[99]" not in result.answer
    assert stored == [], "an invented citation must not become an audit row"


async def test_an_uncited_answer_becomes_an_abstention(account: Account) -> None:
    await seed(account.tenant_id, account.default_label)

    result = await answering(account, MockProvider(["Controllers must do the right thing."]))

    assert result.abstained is True
    assert result.answer == ABSTENTION
    assert result.consulted, "an abstention still says what it looked at (mvp.md 2.10)"


async def test_an_empty_corpus_abstains_without_calling_the_model(account: Account) -> None:
    """Nothing was retrieved, so there is nothing to ground an answer in. Calling the model
    would be asking it to write from its own knowledge — the one thing the prompt forbids —
    and would cost a minute to produce something this system must then refuse to show.
    """
    model = MockProvider(["I happen to know the answer anyway."])

    result = await answering(account, model)

    assert result.abstained is True
    assert result.consulted == []
    assert model.calls == [], "the model must not have been called at all"


async def test_the_model_is_shown_only_passages_this_caller_may_read(account: Account) -> None:
    """The isolation argument for this whole feature, as a test.

    The model sees passage text, so a shortlist that leaked would leak into an answer with a
    citation attached — the same content, now carrying the system's authority.
    """
    await seed(account.tenant_id, account.finance_label)
    narrow = AccessProfile(
        user_id=account.member_id,
        context=TenantContext.for_tenant(account.tenant_id, [account.default_label]),
        permissions=frozenset(CATALOGUE),
    )
    model = MockProvider(["Anything [1]."])
    search = SearchService(narrow, embedder=WorkingEmbedder())  # type: ignore[arg-type]

    result = await AnswerService(narrow, provider=model, search=search).answer(QUESTION)

    assert result.consulted == []
    assert model.calls == [], "the model was never given the finance passages"
    assert result.abstained is True


async def test_the_query_log_is_tenant_scoped(account: Account) -> None:
    """`queries` and `query_citations` are customer data like any other. An audit trail
    readable across tenants would be a worse leak than the documents, because it contains
    the questions people asked."""
    await seed(account.tenant_id, account.default_label)
    result = await answering(account, MockProvider(["Measures [1]."]))

    from app.core.database import tenant_session

    intruder = TenantContext.for_tenant(uuid4())
    async with tenant_session(intruder) as session:
        rows = await session.execute(
            text("SELECT id FROM queries WHERE id = :q"), {"q": result.query_id}
        )
        assert rows.first() is None

    async with tenant_session(TenantContext.for_tenant(account.tenant_id)) as session:
        rows = await session.execute(
            text("SELECT id FROM queries WHERE id = :q"), {"q": result.query_id}
        )
        assert rows.first() is not None


async def test_no_configured_model_is_an_error_rather_than_an_abstention(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Abstention means *the corpus does not answer this*. Saying that when nobody
    configured a model would be a lie about the customer's documents."""
    from app.core.config import settings

    await seed(account.tenant_id, account.default_label)
    monkeypatch.setattr(settings, "llm_endpoint_url", "")
    profile = await profile_for(account)
    search = SearchService(profile, embedder=WorkingEmbedder())  # type: ignore[arg-type]

    with pytest.raises(GenerationUnavailableError, match="llm_config.manage"):
        await AnswerService(profile, search=search).answer(QUESTION)


async def test_a_tenants_own_connector_takes_priority_over_the_default(
    account: Account,
) -> None:
    """The row is read through the tenant's own session — its policy is tenant-scoped, so
    no bypass is needed and the RLS bypass surface stays at four routes."""
    async with owner_session() as session:
        await session.execute(
            text(
                "INSERT INTO llm_config (tenant_id, endpoint_url, model_name) "
                "VALUES (:t, 'http://customer-gateway/v1', 'their-model')"
            ),
            {"t": account.tenant_id},
        )

    profile = await profile_for(account)
    connector = await AnswerService(profile)._resolve()  # type: ignore[reportPrivateUsage]

    assert connector.endpoint_url == "http://customer-gateway/v1"  # type: ignore[attr-defined]
    assert connector.model == "their-model"  # type: ignore[attr-defined]
