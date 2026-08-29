"""Hybrid search against real Postgres, real policies, real indexes.

The tests that matter here are not "does search return something". They are the ones where
a mistake means one customer reads another's documents, or an employee reads a compartment
they are locked out of — and where the mistake would look like a working search.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.common.exceptions import PermissionDeniedError
from app.core.database import owner_session, tenant_session
from app.core.hardware import PROFILES
from app.features.auth.service import AccessProfile
from app.features.embeddings.client import DIMENSION, MODEL, VERSION
from app.features.embeddings.space import SHIPPED, Space
from app.features.retrieval.degradation import SEMANTIC_UNAVAILABLE
from app.features.retrieval.search import RRF_K, candidates, fuse
from app.features.retrieval.service import SearchService
from app.features.tenancy.context import TenantContext
from conftest import Account, LexicalOnlyEmbedder

PASSAGES = [
    ("The controller shall implement appropriate technical measures for data protection.", 1),
    ("Form 1545-0074 must be filed with the annual return by the taxpayer.", 2),
    ("Employees may request access to their personnel records at any time.", 3),
]


async def seed(
    tenant_id: UUID, label_id: UUID, passages: list[tuple[str, int]] | None = None
) -> UUID:
    """A document with chunks and embeddings, written through the owner connection.

    Through the owner deliberately: seeding with the code under test can only prove that the
    code agrees with itself.
    """
    async with owner_session() as session:
        document_id = await session.scalar(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes, status) "
                "VALUES (:t, 'seeded.pdf', :sha, 10, 'ready') RETURNING id"
            ),
            {"t": tenant_id, "sha": str(uuid4())},
        )
        await session.execute(
            text("INSERT INTO document_labels (document_id, label_id) VALUES (:d, :l)"),
            {"d": document_id, "l": label_id},
        )
        await session.execute(
            text(
                "INSERT INTO embedding_spaces (model, version, dimension, status) "
                "VALUES (:m, :v, :d, 'active') ON CONFLICT DO NOTHING"
            ),
            {"m": MODEL, "v": VERSION, "d": DIMENSION},
        )
        for index, (body, page) in enumerate(passages or PASSAGES):
            chunk_id = await session.scalar(
                text(
                    "INSERT INTO chunks (document_id, tenant_id, page_num, char_start, "
                    "char_end, text, bboxes) VALUES (:d, :t, :p, 0, :e, :body, "
                    '\'[{"page": 1, "x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.2}]\') '
                    "RETURNING id"
                ),
                {"d": document_id, "t": tenant_id, "p": page, "e": len(body), "body": body},
            )
            vector = [0.0] * DIMENSION
            vector[index % DIMENSION] = 1.0
            await session.execute(
                text(
                    "INSERT INTO chunk_embeddings (chunk_id, tenant_id, embedding_model, "
                    "embedding_version, embedding) VALUES (:c, :t, :m, :v, "
                    "CAST(:embedding AS vector))"
                ),
                {
                    "c": chunk_id,
                    "t": tenant_id,
                    "m": MODEL,
                    "v": VERSION,
                    "embedding": str(vector),
                },
            )
    return UUID(str(document_id))


async def profile_for(account: Account, labels: tuple[UUID, ...] | None = None) -> AccessProfile:
    from app.features.auth.access.permissions import CATALOGUE

    async with owner_session() as session:
        reachable = tuple(
            await session.scalars(
                text(
                    "SELECT rl.label_id FROM role_labels rl "
                    "JOIN user_roles ur ON ur.role_id = rl.role_id WHERE ur.user_id = :u"
                ),
                {"u": account.admin_id},
            )
        )
    return AccessProfile(
        user_id=account.admin_id,
        context=TenantContext.for_tenant(
            account.tenant_id, labels if labels is not None else reachable
        ),
        permissions=frozenset(CATALOGUE),
    )


def service(profile: AccessProfile) -> SearchService:
    """Search with no embedding service reachable — the lexical half alone.

    The dense half needs vectors that mean something, which needs a real model. Its wiring
    is covered by `test_the_dense_half_is_label_filtered_by_its_join`, which supplies a
    vector directly.
    """
    return SearchService(profile, embedder=LexicalOnlyEmbedder())  # type: ignore[arg-type]


async def test_lexical_search_finds_a_passage(account: Account) -> None:
    await seed(account.tenant_id, account.default_label)

    result = await service(await profile_for(account)).search("technical measures")

    assert [hit.page_num for hit in result.hits] == [1]
    assert result.hits[0].lexical_rank == 1
    assert result.hits[0].bboxes


async def test_an_exact_identifier_is_found(account: Account) -> None:
    """The reason the lexical half exists at all.

    M0 measured this directly: with a regex tokeniser the query side destroyed identifiers
    the index held whole, lexical search found one in six, and the conclusion nearly drawn
    was that ParadeDB was not worth its dependency. Two of M0's identifier questions are
    answerable *only* by this half.
    """
    await seed(account.tenant_id, account.default_label)

    result = await service(await profile_for(account)).search("1545-0074")

    assert [hit.page_num for hit in result.hits] == [2]


async def test_a_query_with_no_lexemes_returns_nothing_rather_than_erroring(
    account: Account,
) -> None:
    """`to_tsquery` raises on an empty string. A question of pure punctuation is a user
    typing badly, not a 500."""
    await seed(account.tenant_id, account.default_label)

    result = await service(await profile_for(account)).search("?? !!")

    assert result.hits == []


async def test_a_tenant_cannot_search_another_tenants_corpus(account: Account) -> None:
    """The single most important assertion in this feature.

    Run through the intruder's own session against real policies — not with a filter in the
    query, which is exactly what this design refuses to rely on.
    """
    await seed(account.tenant_id, account.default_label)
    from app.features.auth.access.permissions import CATALOGUE

    intruder = AccessProfile(
        user_id=uuid4(),
        context=TenantContext.for_tenant(uuid4()),
        permissions=frozenset(CATALOGUE),
    )

    result = await service(intruder).search("technical measures")

    assert result.hits == []


async def test_a_passage_behind_an_unreachable_label_is_invisible(account: Account) -> None:
    """Chunks carry their own copy of the document's labels, filled by the F3 trigger.

    Without that, a passage from a Finance document would be retrievable by everyone while
    the document itself stayed correctly restricted — a leak with no visible symptom.
    """
    await seed(account.tenant_id, account.finance_label)
    reaching_only_the_default = await profile_for(account, labels=(account.default_label,))

    result = await service(reaching_only_the_default).search("technical measures")

    assert result.hits == []


async def test_narrowing_to_a_label_you_hold_filters_the_results(account: Account) -> None:
    await seed(account.tenant_id, account.finance_label)
    await seed(account.tenant_id, account.default_label, [("Unrelated general guidance.", 9)])
    profile = await profile_for(account)

    everything = await service(profile).search("guidance measures")
    narrowed = await service(profile).search("guidance measures", labels=[account.finance_label])

    assert len(everything.hits) >= len(narrowed.hits)
    assert all(hit.page_num != 9 for hit in narrowed.hits)


async def test_narrowing_to_a_label_you_do_not_hold_is_a_403(account: Account) -> None:
    """Not an empty result. An empty result teaches a client to probe other people's labels
    and watch which ones come back silent."""
    profile = await profile_for(account, labels=(account.default_label,))

    with pytest.raises(PermissionDeniedError):
        await service(profile).search("anything", labels=[account.finance_label])


async def test_an_unreachable_embedding_service_degrades_rather_than_failing(
    account: Account,
) -> None:
    """The decision recorded in the F6 design, as behaviour.

    Half a search in two seconds beats a whole one in forty — but only if the caller can
    tell which one they received.
    """
    await seed(account.tenant_id, account.default_label)

    result = await service(await profile_for(account)).search("technical measures")

    assert result.degraded is True
    assert result.reason == SEMANTIC_UNAVAILABLE
    assert result.hits, "the lexical half must still answer"


async def test_the_dense_half_is_label_filtered_by_its_join(account: Account) -> None:
    """`chunk_embeddings` is filtered by tenant only — the join to `chunks` is what enforces
    labels, and dropping it to "simplify" the query would return passages from documents the
    caller cannot open.

    Asserted against the query itself, with a vector supplied directly, because no
    higher-level test would notice the difference.
    """
    from app.features.retrieval.search import dense

    await seed(account.tenant_id, account.finance_label)
    narrow = TenantContext.for_tenant(account.tenant_id, [account.default_label])

    async with tenant_session(narrow) as session:
        found = await dense(session, [0.0] * DIMENSION, SHIPPED, 50, 40)

    assert found == []


async def test_the_unscoped_dense_query_asks_for_an_iterative_scan(account: Account) -> None:
    """The hot path, where the filter is invisible because nobody wrote it.

    `hnsw.iterative_scan` used to be set only when a document scope was passed, on the
    reasoning that a query with no scope has no filter and therefore nothing to discard.
    RLS is a filter, and it is on every query: the HNSW graph is shared by every tenant, so
    the scan takes its `ef_search` nearest neighbours from all of it and the policies throw
    rows away afterwards. Measured on the real corpus at the tenant scope, the unscoped
    dense half returned 43.1 of 50 candidates and *nothing at all* for 5 of 42 questions,
    with no error anywhere — `eval/tenant-scale.json`, `eval/iterative-scan.json`.

    Asserted on the session setting rather than on a row count, because the failure cannot
    be reproduced at this corpus's size and that is the whole reason it survived: below
    roughly 15% of the graph the planner abandons HNSW for an exact scan, the post-filter
    problem disappears, and a test that seeded three chunks would pass against the broken
    code. What is checkable everywhere is that the query asked for the scan.

    `documents=None` is the point of the test. Make this conditional again — on a scope, on
    a profile field, on anything — and it fails here.
    """
    from app.features.retrieval.search import ITERATIVE_SCAN, dense

    await seed(account.tenant_id, account.default_label)
    context = (await profile_for(account)).context

    async with tenant_session(context) as session:
        await dense(session, [0.0] * DIMENSION, SHIPPED, 50, 100, documents=None)
        requested = await session.scalar(text("SELECT current_setting('hnsw.iterative_scan')"))

    assert requested == ITERATIVE_SCAN, (
        f"an unscoped dense query left hnsw.iterative_scan at {requested!r}: the scan stops "
        f"at ef_search neighbours and RLS discards from there, so the dense half returns "
        "fewer than the 50 candidates fusion was promised and nothing reports it"
    )


async def test_only_the_active_embedding_space_is_searched(account: Account) -> None:
    """Vectors from two models are not comparable. During a reindex both exist, and a query
    that mixed them would rank across incompatible spaces and return confident nonsense."""
    from app.features.retrieval.search import dense

    await seed(account.tenant_id, account.default_label)
    context = (await profile_for(account)).context

    async with tenant_session(context) as session:
        other_space = await dense(
            session,
            [0.0] * DIMENSION,
            Space("some/other-model", "1", DIMENSION, None),
            50,
            40,
        )
        this_space = await dense(session, [0.0] * DIMENSION, SHIPPED, 50, 40)

    assert other_space == []
    assert this_space


def test_rrf_prefers_agreement_over_a_single_strong_opinion() -> None:
    """The property that makes fusion worth doing.

    A chunk both halves rank third beats one that is first in a single list and absent from
    the other. That is the whole argument for hybrid search: two weak agreements are better
    evidence than one confident half.
    """
    agreed, lexical_only = uuid4(), uuid4()
    ranking = fuse([lexical_only, agreed], [agreed], limit=2)

    assert ranking[0][0] == agreed
    assert ranking[0][1] == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1))


def test_rrf_uses_positions_never_scores() -> None:
    """`ts_rank_cd` and cosine distance live on different, corpus-dependent scales.
    Normalising them is fragile and needs recalibrating whenever the data changes; RRF never
    sees a magnitude at all."""
    first, second = uuid4(), uuid4()

    assert fuse([first, second], [], 2)[0][1] == pytest.approx(1 / (RRF_K + 1))


async def test_an_identifier_survives_a_dense_half_that_disagrees(account: Account) -> None:
    """Milestone 1's acceptance case: an exact alphanumeric id wins even when meaning does
    not.

    The embedding stub points at chunk 0 — the data-protection passage — so the dense half
    ranks the identifier's chunk last or not at all, which is exactly what a real embedder
    does with a question that is a bare serial number: it has no semantics to work with.
    Only the lexical and exact halves can find it, and the fused result has to keep it
    anyway.

    This is the case RRF alone gets wrong, and it is why `_promote_leaders` exists: agreement
    between two mediocre semantic matches outscores a single decisive exact one, so the right
    passage is found, ranked, and then buried by arithmetic.
    """
    from conftest import WorkingEmbedder

    await seed(account.tenant_id, account.default_label)
    profile = await profile_for(account)

    # A profile without a cross-encoder, stated rather than inherited. This test is about
    # what fusion does with the two halves, and `degraded` is asserted below — left to the
    # ambient profile it would instead report whether a reranker happened to be listening,
    # which passes on a developer's machine with `ZENITH_HARDWARE=low-spec` in `.env` and
    # fails in CI for a reason that has nothing to do with ranking.
    result = await SearchService(
        profile,
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        hardware=PROFILES["low-spec"],
    ).search("1545-0074", limit=3)

    assert result.hits, "an exact identifier must be findable"
    assert result.hits[0].page_num == 2, "and it must be first, not merely present"
    assert not result.degraded, "both halves ran; this is not a fallback"


def test_the_leader_floor_survives_the_reranker_budget() -> None:
    """The bug that put the Código Penal where the Constitución belonged.

    `_promote_leaders` guarantees each half's top hit a place. It is applied inside `fuse`,
    against the limit `fuse` was given — so calling `candidates()` with no limit and slicing
    the result afterwards makes it a no-op: nothing is ever missing from an unbounded list,
    so nothing is promoted, and the slice is a plain RRF top-N.

    That is not a hypothetical ordering. RRF rewards agreement, so seven passages ranked
    mediocrely by both halves outscore one ranked *first* by a single half — 1/61 against
    1/106 + 1/83 — and the single-half leader falls outside a budget of eight.

    Asserted on the arithmetic rather than on a corpus: the dense leader here appears in no
    other ranking, which is exactly the shape RRF discards and the reranker most needs to
    see.
    """
    dense_leader = uuid4()
    shared = [uuid4() for _ in range(8)]

    # Every shared chunk is ranked by both halves, so each collects two reciprocal scores
    # and outranks the leader's single one.
    result = candidates(
        lexical_ids=[*shared],
        dense_ids=[dense_leader, *shared],
        exact_ids=[],
        limit=8,
    )

    assert len(result) == 8
    assert dense_leader in {chunk_id for chunk_id, _ in result}, (
        "the dense half's first choice must survive the reranker's budget"
    )
