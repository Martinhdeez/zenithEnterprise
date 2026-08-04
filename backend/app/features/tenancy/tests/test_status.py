"""Tenant status against real policies.

The interesting assertion is not that the counts are right — it is that they are *this
tenant's* counts, decided by RLS rather than by a filter someone remembered to write.
"""

from uuid import uuid4

from app.core.hardware import PROFILES
from app.features.retrieval.tests.test_search import seed
from app.features.tenancy.context import TenantContext
from app.features.tenancy.status import status
from conftest import Account


async def test_the_counts_describe_the_corpus(account: Account) -> None:
    await seed(account.tenant_id, account.default_label)

    reported = await status(TenantContext.for_tenant(account.tenant_id, [account.default_label]))

    assert reported.documents["ready"] == 1
    assert reported.chunks == 3
    assert reported.searchable is True


async def test_the_counts_are_label_scoped_not_merely_tenant_scoped(account: Account) -> None:
    """A security property, not a display preference.

    mvp.md 3.1: a user who cannot reach a document must also be unable to *deduce that it
    exists*. A status route reporting "41 documents" to someone who can open two would leak
    the size of a compartment they are locked out of — and it would do it on the first
    screen, to every user, on every page load.

    Nothing here filters by label. The policies do it, which is why this passes.
    """
    await seed(account.tenant_id, account.finance_label)

    reaching_elsewhere = await status(
        TenantContext.for_tenant(account.tenant_id, [account.default_label])
    )

    assert reaching_elsewhere.documents == {}
    assert reaching_elsewhere.chunks == 0
    assert reaching_elsewhere.searchable is False


async def test_another_tenant_sees_nothing(account: Account) -> None:
    """The assertion that matters. Corpus size is commercially interesting on its own — how
    many documents a competitor holds is not a number they should be able to read — and it
    is exactly the kind of aggregate a `WHERE` clause forgets."""
    await seed(account.tenant_id, account.default_label)

    reported = await status(TenantContext.for_tenant(uuid4()))

    assert reported.documents == {}
    assert reported.chunks == 0
    assert reported.searchable is False


async def test_an_empty_tenant_is_not_searchable(account: Account) -> None:
    """`searchable` is computed here so every client agrees what an empty corpus is. A UI
    left to derive it will derive it differently in three places, and one of them will show
    a search box that can only ever return nothing."""
    reported = await status(TenantContext.for_tenant(account.tenant_id))

    assert reported.searchable is False


async def test_components_follow_the_hardware_profile(account: Account) -> None:
    """Configuration, not liveness. A customer on `low-spec` has no reranker by choice, and
    a client showing "reranking disabled" needs to be able to say why."""
    low = await status(TenantContext.for_tenant(account.tenant_id), PROFILES["low-spec"])
    cpu = await status(TenantContext.for_tenant(account.tenant_id), PROFILES["cpu"])

    assert low.hardware == "low-spec"
    assert low.components.reranker is False
    assert cpu.components.reranker is True
    assert low.components.embeddings is True, "ingestion cannot run without one"


async def test_failed_documents_are_reported_rather_than_hidden(account: Account) -> None:
    """A document that failed to ingest is the single most useful thing on this endpoint.

    It is invisible in search — that is what failing means — so if the status route does not
    surface it, the customer's only symptom is an answer that should have existed and did
    not.
    """
    from sqlalchemy import text

    from app.core.database import owner_session

    async with owner_session() as session:
        await session.execute(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes, status) "
                "VALUES (:t, 'broken.pdf', :sha, 10, 'failed')"
            ),
            {"t": account.tenant_id, "sha": str(uuid4())},
        )

    reported = await status(TenantContext.for_tenant(account.tenant_id))

    assert reported.documents["failed"] == 1
