"""The reranker and the fusion correction it exposed."""

from uuid import uuid4

import httpx
import pytest

from app.core.hardware import PROFILES
from app.features.auth.service import AccessProfile
from app.features.retrieval.degradation import RERANKING_UNAVAILABLE
from app.features.retrieval.reranker import RerankerUnavailable, TeiReranker
from app.features.retrieval.search import RRF_K, candidates, fuse
from app.features.retrieval.service import SearchService
from app.features.tenancy.context import TenantContext
from conftest import Account, WorkingEmbedder

from .test_search import profile_for, seed

# A query that matches all three seeded passages, so there is an order to disturb. With one
# match, a reranker that did nothing and one that worked perfectly would look identical.
QUERY = "controller taxpayer employees"


def reranker(handler: object, profile: str = "cpu") -> TeiReranker:
    return TeiReranker(
        url="http://rerank",
        profile=PROFILES[profile],
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


def reverse_order(request: httpx.Request) -> httpx.Response:
    """Scores that reverse whatever order it was handed.

    A stub that agreed with the incoming order could not tell a working reranker from one
    that was never called.
    """
    payload = httpx.Response(200, content=request.content).json()
    # The score decides, not the order of the response body — `rank` sorts. An earlier
    # version scored the first item highest and reversed the list, which reversed nothing.
    return httpx.Response(
        200,
        json=[{"index": index, "score": float(index)} for index in range(len(payload["texts"]))],
    )


async def test_the_cross_encoder_decides_the_final_order(account: Account) -> None:
    """Compared against the *unrestricted* fused order, deliberately.

    Comparing against `limit=3` would compare two different things: the leader promotion in
    `fuse` only has an effect when the limit cuts a leader off, so a short fused list is not
    a prefix of a long one. Asking for the whole candidate set removes that difference and
    leaves only the reranker's contribution.
    """
    await seed(account.tenant_id, account.default_label)
    profile = await profile_for(account)

    fused = await SearchService(
        profile,
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        reranker=None,
        hardware=PROFILES["low-spec"],
    ).search(QUERY, limit=50)
    reranked = await SearchService(
        profile,
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        reranker=reranker(reverse_order),
    ).search(QUERY, limit=3)

    assert [hit.chunk_id for hit in reranked.hits] == [
        hit.chunk_id for hit in reversed(fused.hits)
    ][:3], "the reranker did not reorder anything"
    assert reranked.degraded is False


async def test_a_reranker_that_is_down_degrades_to_the_fused_order(account: Account) -> None:
    """A component being unreachable must not take a working search away from a customer.

    The fused order was the entire product one commit ago. Falling back to it and saying so
    is the honest answer; a 503 would report a component the user did not know existed.
    """
    await seed(account.tenant_id, account.default_label)

    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    result = await SearchService(
        await profile_for(account),
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        reranker=reranker(dead),
    ).search(QUERY)

    assert result.hits, "the search must still answer"
    assert result.degraded is True
    assert result.reason == RERANKING_UNAVAILABLE


async def test_a_profile_with_the_reranker_off_is_not_degraded(account: Account) -> None:
    """The distinction that stops `degraded` from crying wolf.

    On `low-spec` the reranker is off by configuration and `zenith diagnose` names it. A
    customer who chose that profile is not experiencing a failure; one whose reranker
    crashed is, and the two must not look the same.
    """
    await seed(account.tenant_id, account.default_label)

    result = await SearchService(
        await profile_for(account),
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        hardware=PROFILES["low-spec"],
    ).search(QUERY)

    assert result.hits
    assert result.degraded is False


async def test_a_rejected_batch_is_reported_rather_than_retried(account: Account) -> None:
    """A 413 will be a 413 every time, and this is the interactive path."""
    calls = 0

    def rejects(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(413, json={"error": "batch too large"})

    with pytest.raises(RerankerUnavailable, match="ZENITH_HARDWARE"):
        await reranker(rejects).rank("question", ["passage"])

    assert calls == 1


async def test_batches_account_for_the_question_in_every_pair() -> None:
    """A rerank request carries the question alongside *every* passage.

    Reusing the embedding budget without counting that is how M0's 413 comes back wearing a
    different hat: eight 1,200-character passages cost more here than eight chunks did
    during ingestion.
    """
    profile = PROFILES["low-spec"]
    client = TeiReranker(url="http://rerank", profile=profile)
    question = "x" * 900  # ~300 estimated tokens, charged once per pair
    passages = ["y" * 900] * 12

    batches = client.plan_batches(question, passages)

    for start, batch in batches:
        cost = sum(len(text) // 3 + 1 + len(question) // 3 + 1 for text in batch)
        assert cost <= profile.max_batch_tokens, f"batch at {start} exceeds the budget"
    assert sum(len(batch) for _, batch in batches) == len(passages)


def test_a_single_half_leader_is_not_ranked_out_of_existence() -> None:
    """The measured fusion bug.

    An exact identifier match is by nature a passage only the lexical half can find, and RRF
    rewards agreement:

        ranked 1st lexically, absent from dense →  1/61  = 0.0164
        ranked 5th by both                      →  2/65  = 0.0308   ← wins

    Identifier questions scored 0% at rank 8 for exactly this reason.
    """
    identifier_match = uuid4()
    agreed = [uuid4() for _ in range(8)]

    ranked = fuse([identifier_match, *agreed], agreed, limit=4)

    assert identifier_match in [chunk_id for chunk_id, _ in ranked]


def test_promotion_takes_the_last_place_not_the_first() -> None:
    """Being one half's favourite is evidence, not proof.

    The promoted entry joins the result rather than displacing the top of it, so the fused
    order is untouched for everything else.
    """
    lexical_leader = uuid4()
    agreed = [uuid4() for _ in range(8)]

    ranked = [chunk_id for chunk_id, _ in fuse([lexical_leader, *agreed], agreed, limit=4)]

    assert ranked[-1] == lexical_leader
    assert ranked[:3] == agreed[:3]


def test_the_candidate_set_is_the_union_of_both_halves() -> None:
    """Choosing candidates and ordering results are different problems.

    A passage only one half found is exactly the case worth reranking, and exactly the case
    the fused top-N discards. Two of six identifier questions were not in the fused top-50
    at all — no reranker could have helped them.
    """
    lexical_ids = [uuid4() for _ in range(50)]
    dense_ids = [uuid4() for _ in range(50)]

    shortlist = [chunk_id for chunk_id, _ in candidates(lexical_ids, dense_ids)]

    assert set(shortlist) == set(lexical_ids) | set(dense_ids)
    assert len(shortlist) == 100


def test_fusion_still_prefers_agreement_where_nothing_is_promoted() -> None:
    """The correction must not become a rule that single-half hits win.

    Where both leaders are already present, the ordering is untouched RRF: two weak
    agreements still beat one confident half.
    """
    agreed, lexical_only = uuid4(), uuid4()

    ranking = fuse([lexical_only, agreed], [agreed], limit=2)

    assert ranking[0][0] == agreed
    assert ranking[0][1] == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1))


async def test_the_reranker_cannot_widen_what_rls_released(account: Account) -> None:
    """It reads passage text the policies already released, so it cannot add a row.

    Stated as a test because "it only reorders" is the kind of claim that stops being true
    the moment someone adds a lookup to enrich a candidate.
    """
    await seed(account.tenant_id, account.finance_label)
    narrow = AccessProfile(
        user_id=account.member_id,
        context=TenantContext.for_tenant(account.tenant_id, [account.default_label]),
        permissions=frozenset({"query.execute"}),
    )

    result = await SearchService(
        narrow,
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        reranker=reranker(reverse_order),
    ).search(QUERY)

    assert result.hits == []
