"""Folder aggregation, and the leak it would be if the client computed it.

mvp.md 2.14 lists folder hierarchies as a non-goal, and this is not one: nothing here can
be created, moved or nested. It is the label structure the tenant already has, grouped into
the shape a sidebar wants.

The assertion that matters is the isolation one. A folder the caller cannot reach must be
absent rather than empty — an empty folder named "Finance" tells a member that Finance
exists and has something in it, which is precisely the inference mvp.md 3.1 forbids.
"""

from uuid import UUID, uuid4

from sqlalchemy import text

from app.core.database import owner_session
from app.features.documents.folders import UNLABELLED, tree
from app.features.tenancy.context import TenantContext
from conftest import Account


async def document(
    tenant_id: UUID, label_id: UUID | None, status: str = "ready", name: str = "doc.pdf"
) -> UUID:
    """A document, written through the owner connection so the fixture cannot agree with
    the code under test by construction."""
    async with owner_session() as session:
        document_id = await session.scalar(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes, status) "
                "VALUES (:t, :name, :sha, 10, :status) RETURNING id"
            ),
            {"t": tenant_id, "name": name, "sha": uuid4().hex + uuid4().hex[:32], "status": status},
        )
        if label_id:
            await session.execute(
                text("INSERT INTO document_labels (document_id, label_id) VALUES (:d, :l)"),
                {"d": document_id, "l": label_id},
            )
    return UUID(str(document_id))


async def test_documents_are_grouped_by_label(account: Account) -> None:
    await document(account.tenant_id, account.default_label, name="one.pdf")
    await document(account.tenant_id, account.default_label, name="two.pdf")
    await document(account.tenant_id, account.finance_label, name="three.pdf")

    computed = await tree(
        TenantContext.for_tenant(account.tenant_id, [account.default_label, account.finance_label])
    )
    by_name = {folder.name: folder for folder in computed.folders}

    assert by_name["General"].documents == 2
    assert by_name["Finance"].documents == 1
    assert computed.total_documents == 3


async def test_a_folder_the_caller_cannot_reach_is_absent_not_empty(account: Account) -> None:
    """The assertion this endpoint lives or dies on.

    An empty folder named "Finance" tells a member that Finance exists and has something
    in it. mvp.md 3.1: a user who cannot reach a document must not be able to deduce that
    it exists — not through citations, not through listings, and not through a sidebar.
    """
    await document(account.tenant_id, account.finance_label)

    computed = await tree(TenantContext.for_tenant(account.tenant_id, [account.default_label]))

    assert "Finance" not in {folder.name for folder in computed.folders}
    assert computed.total_documents == 0


async def test_unlabelled_documents_get_their_own_bucket(account: Account) -> None:
    """A document with no labels is visible to the whole tenant, and on a new installation
    that is most of the corpus. A tree that omitted them would look broken."""
    await document(account.tenant_id, None)

    computed = await tree(TenantContext.for_tenant(account.tenant_id, [account.default_label]))
    unlabelled = next(folder for folder in computed.folders if folder.name == UNLABELLED)

    assert unlabelled.label_id is None, "it is a bucket, not a label"
    assert unlabelled.documents == 1


async def test_a_label_with_no_documents_is_omitted(account: Account) -> None:
    """An empty folder in a sidebar is a place to click that does nothing. The full label
    list is `GET /labels`, for the screens that manage labels rather than browse them."""
    computed = await tree(TenantContext.for_tenant(account.tenant_id, [account.default_label]))

    assert computed.folders == []


async def test_processing_and_failed_are_counted_separately(account: Account) -> None:
    """A folder with three ready documents and one failed is a different thing from one
    with four, and the failed document is invisible in search — this count is the only
    place its absence can be explained."""
    await document(account.tenant_id, account.default_label, status="ready")
    # A real in-flight status. `processing` is not one — the constraint rejects it,
    # which is how F16 discovered a folder count filtering on a status that has never
    # existed and therefore matched nothing, silently.
    await document(account.tenant_id, account.default_label, status="embedding")
    await document(account.tenant_id, account.default_label, status="failed")

    computed = await tree(TenantContext.for_tenant(account.tenant_id, [account.default_label]))
    general = next(folder for folder in computed.folders if folder.name == "General")

    assert (general.documents, general.ready, general.processing, general.failed) == (3, 1, 1, 1)


async def test_another_tenant_sees_nothing(account: Account) -> None:
    await document(account.tenant_id, account.default_label)

    computed = await tree(TenantContext.for_tenant(uuid4()))

    assert computed.folders == []


async def test_the_default_label_sorts_first(account: Account) -> None:
    """Uploads with no label specified land in the default one, so it is where a new user
    will look for what they just added."""
    await document(account.tenant_id, account.finance_label)
    await document(account.tenant_id, account.default_label)

    computed = await tree(
        TenantContext.for_tenant(account.tenant_id, [account.default_label, account.finance_label])
    )

    assert computed.folders[0].is_default is True
