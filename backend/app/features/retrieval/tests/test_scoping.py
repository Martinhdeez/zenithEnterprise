"""Scoping a question to named documents — the `@` mention, at the retrieval layer.

Two properties, and the second is the one worth the file. The first is that the filter
works: a question scoped to one document is answered from that document. The second is that
it is a **filter and nothing more** — it narrows the set the policies already produced, so
it can never be a way to read something. A test that only proved the first would pass on an
implementation that scoped by putting the caller's document ids into the query instead of
into the policy, which is the same shape of mistake this system refuses everywhere else.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.common.exceptions import PermissionDeniedError
from app.core.database import owner_session, tenant_session
from app.features.auth.permissions import CATALOGUE
from app.features.auth.service import AccessProfile
from app.features.embeddings.client import DIMENSION, MODEL, VERSION
from app.features.retrieval.service import SearchService
from app.features.tenancy.context import TenantContext
from conftest import Account, LexicalOnlyEmbedder

from .test_search import profile_for, seed, service

pytestmark = pytest.mark.asyncio

# Two documents whose facts do not overlap. The question below matches a passage in each,
# so an unscoped search finds both and any single-document answer is the scope working
# rather than the corpus being small.
POLICY = [("The severance allowance is twenty days of salary per year.", 1)]
HANDBOOK = [("The severance allowance is thirty days of salary per year.", 1)]


async def test_scoping_to_one_document_excludes_the_other(account: Account) -> None:
    policy = await seed(account.tenant_id, account.default_label, POLICY)
    handbook = await seed(account.tenant_id, account.default_label, HANDBOOK)
    searcher = service(await profile_for(account))

    everything = await searcher.search("severance allowance")
    scoped = await searcher.search("severance allowance", documents=[policy])

    assert {hit.document_id for hit in everything.hits} == {policy, handbook}
    assert {hit.document_id for hit in scoped.hits} == {policy}
    assert all("thirty" not in hit.text for hit in scoped.hits)


async def test_a_fact_only_in_the_excluded_document_is_unreachable(account: Account) -> None:
    """The claim the feature actually makes to a user.

    Not "the other document ranks lower" — gone. A scoped question that still returns a
    passage from an unmentioned file has silently handed the model context the user
    explicitly excluded, and the answer it grounds would be indistinguishable from a
    correct one.
    """
    await seed(account.tenant_id, account.default_label, POLICY)
    handbook = await seed(account.tenant_id, account.default_label, HANDBOOK)

    scoped = await service(await profile_for(account)).search(
        "severance allowance", documents=[handbook]
    )

    assert scoped.hits
    assert all(hit.document_id == handbook for hit in scoped.hits)
    assert all("twenty" not in hit.text for hit in scoped.hits)


async def test_scoping_to_several_documents_returns_all_of_them(account: Account) -> None:
    policy = await seed(account.tenant_id, account.default_label, POLICY)
    handbook = await seed(account.tenant_id, account.default_label, HANDBOOK)
    await seed(account.tenant_id, account.default_label, [("Unrelated severance note.", 4)])

    scoped = await service(await profile_for(account)).search(
        "severance allowance", documents=[policy, handbook]
    )

    assert {hit.document_id for hit in scoped.hits} == {policy, handbook}


async def test_an_identifier_still_reaches_its_chunk_inside_the_scope(account: Account) -> None:
    """The exact-identifier query is a third SQL statement, and it needed the filter too.

    Left unscoped it would keep its own path into the candidate union, and a passage from an
    unmentioned document would arrive through it while the other two halves were correctly
    filtered — the leak would appear only for questions containing a number.
    """
    scoped_document = await seed(
        account.tenant_id, account.default_label, [("Catalog Number 10000W applies here.", 2)]
    )
    await seed(
        account.tenant_id, account.default_label, [("Catalog Number 10000W is also here.", 7)]
    )

    result = await service(await profile_for(account)).search(
        "What is Catalog Number 10000W?", documents=[scoped_document]
    )

    assert result.hits
    assert all(hit.document_id == scoped_document for hit in result.hits)


async def test_scoping_to_another_tenants_document_is_refused(account: Account) -> None:
    """The isolation assertion, and it must fail loudly rather than quietly.

    Under RLS the id matches nothing, so an empty result would already be safe — but it
    would be reported to the caller as "that document contains no answer", a claim about
    the corpus rather than about permissions, and the wrong one.
    """
    async with owner_session() as session:
        stranger = await session.scalar(
            text("INSERT INTO tenants (name) VALUES (:name) RETURNING id"),
            {"name": f"Stranger {uuid4().hex[:8]}"},
        )
        elsewhere = await session.scalar(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes, status) "
                "VALUES (:t, 'theirs.pdf', :sha, 10, 'ready') RETURNING id"
            ),
            {"t": stranger, "sha": str(uuid4())},
        )

    with pytest.raises(PermissionDeniedError):
        await service(await profile_for(account)).search("anything", documents=[elsewhere])


async def test_scoping_to_a_document_behind_an_unreachable_label_is_refused(
    account: Account,
) -> None:
    """The same rule one level down: same tenant, wrong compartment.

    The caller holds `default` only, and the document is filed under `finance`. RLS hides
    it, and the refusal is what tells the client the request was nonsense rather than
    unlucky.
    """
    restricted = await seed(account.tenant_id, account.finance_label, POLICY)
    reaching_only_the_default = await profile_for(account, labels=(account.default_label,))

    with pytest.raises(PermissionDeniedError):
        await service(reaching_only_the_default).search("severance", documents=[restricted])


async def test_scoping_cannot_widen_a_label_narrowing(account: Account) -> None:
    """Two filters, both narrowing, applied together rather than one overriding the other.

    A caller who narrows to `finance` and then names a `default` document must get nothing —
    the document is outside the labels this search was told to use. An implementation where
    the document filter replaced the label narrowing would return it.

    Empty rather than a 403: the caller *can* read this document, and has just asked to look
    away from it. That is a contradiction in the request, not a permission failure, and the
    two are different messages.
    """
    general = await seed(account.tenant_id, account.default_label, POLICY)
    profile = await profile_for(account)

    scoped = await service(profile).search(
        "severance allowance", labels=[account.finance_label], documents=[general]
    )

    assert scoped.hits == []


async def test_an_unscoped_search_is_unchanged(account: Account) -> None:
    """The regression guard for the 99% of questions that name no document.

    The filter is built by string concatenation onto the `WHERE` clause, so "no documents"
    has to produce exactly the query that existed before — including no `:documents`
    parameter, which SQLAlchemy would otherwise reject as unbound.
    """
    await seed(account.tenant_id, account.default_label, POLICY)

    nothing: list[list[UUID] | None] = [None, []]
    for empty in nothing:
        result = await service(await profile_for(account)).search(
            "severance allowance", documents=empty
        )
        assert len(result.hits) == 1


async def test_the_dense_half_is_scoped_by_its_own_query(account: Account) -> None:
    """Asserted against the SQL directly, with a vector supplied by hand.

    The dense half is the one where a filter is easy to get wrong and impossible to see: it
    ranks by distance, so a scope that failed to apply would still return plausible
    passages, in a sensible order, from documents nobody named.
    """
    from app.features.retrieval.search import dense

    scoped_document = await seed(account.tenant_id, account.default_label, POLICY)
    await seed(account.tenant_id, account.default_label, HANDBOOK)
    context = (await profile_for(account)).context

    async with tenant_session(context) as session:
        everything = await dense(session, [1.0] + [0.0] * (DIMENSION - 1), MODEL, VERSION, 50, 40)
        scoped = await dense(
            session,
            [1.0] + [0.0] * (DIMENSION - 1),
            MODEL,
            VERSION,
            50,
            40,
            documents=[scoped_document],
        )

    assert len(everything) == 2
    assert len(scoped) == 1


async def test_an_intruder_gains_nothing_by_naming_a_real_id(account: Account) -> None:
    """The attack the parameter invites: a valid document id from another tenant.

    Refused before it reaches retrieval, and refused by the policies underneath even if it
    were not — the id is looked up inside the intruder's own `tenant_session`, where it does
    not exist.
    """
    document = await seed(account.tenant_id, account.default_label, POLICY)
    intruder = AccessProfile(
        user_id=uuid4(),
        context=TenantContext.for_tenant(uuid4()),
        permissions=frozenset(CATALOGUE),
    )

    with pytest.raises(PermissionDeniedError):
        await SearchService(intruder, embedder=LexicalOnlyEmbedder()).search(  # type: ignore[arg-type]
            "severance allowance", documents=[document]
        )
