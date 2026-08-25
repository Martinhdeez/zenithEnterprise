"""Who can read a document nobody has classified yet.

Migration 0017 answers that with a quarantine label only `admin` reaches, plus one clause in
the `documents` policy that lets an uploader read their own document while it is still
ingesting. That clause is the first time a document is reachable by something other than a
label, so it gets the isolation matrix a new access route deserves rather than a single happy
path — the point of these tests is the four things the exception must *not* do.

The uploader here is deliberately not an administrator. `member` holds no `documents.upload`,
so the case the exception exists for is a custom role — an "editor" — that may upload and does
not reach the quarantine label. An administrator would see the document through the label and
prove nothing about the clause.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.database import owner_session, tenant_session
from app.features.auth.service import AccessProfile
from app.features.documents.service import DocumentService
from app.features.documents.storage import DocumentStorage
from app.features.tenancy.context import TenantContext
from conftest import Account

PDF = b"%PDF-1.7\nnot a real document, but it starts like one\n"


async def pdf(content: bytes = PDF) -> AsyncIterator[bytes]:
    yield content


@pytest.fixture
def storage(tmp_path: Path) -> DocumentStorage:
    return DocumentStorage(root=tmp_path / "storage")


def editor(account: Account) -> AccessProfile:
    """May upload; reaches the tenant default and nothing else."""
    return AccessProfile(
        user_id=account.member_id,
        context=TenantContext.for_tenant(account.tenant_id, (account.default_label,)),
        permissions=frozenset({"documents.upload"}),
    )


def reading(account: Account) -> TenantContext:
    """The editor's own context as the API builds it.

    `AccessProfile.context` carries no user id — most policies decide everything from the
    tenant and the labels — so `DocumentService` binds it, and a reader that skips that step
    is testing a session the product never opens. That omission is exactly the failure this
    exception has: it fails closed and shows the uploader nothing.
    """
    return TenantContext.for_tenant(
        account.tenant_id, (account.default_label,), user_id=account.member_id
    )


def colleague(account: Account) -> TenantContext:
    """Someone else in the same tenant, holding the same default label as the editor.

    The comparison that matters: identical labels, different person. Anything the editor can
    see that this context cannot is the exception doing its work, and anything *this* context
    can see is the leak the whole change was about.
    """
    return TenantContext.for_tenant(
        account.tenant_id, (account.default_label,), user_id=account.admin_id
    )


async def visible(context: TenantContext, document_id: UUID) -> bool:
    async with tenant_session(context) as session:
        return (
            await session.scalar(text("SELECT 1 FROM documents WHERE id = :d"), {"d": document_id})
        ) is not None


async def mark_ready(document_id: UUID) -> None:
    async with owner_session() as session:
        await session.execute(
            text("UPDATE documents SET status = 'ready' WHERE id = :d"), {"d": document_id}
        )


async def upload(account: Account, storage: DocumentStorage) -> UUID:
    result = await DocumentService(editor(account), storage).upload("unfiled.pdf", pdf())
    assert set(result.labels) == {account.quarantine_label}, "an unnamed upload must quarantine"
    return result.document.id


# --- what the quarantine label is for -------------------------------------------------


async def test_a_colleague_cannot_see_an_unfiled_upload(
    account: Account, storage: DocumentStorage
) -> None:
    """The window this change closes.

    Before 0017 the document carried the tenant default, which `member` reaches, so it was
    readable by everybody from the moment the upload answered until the classifier ran at the
    end of ingestion — minutes on `low-spec`, covering the document list and the PDF download
    and not merely search.
    """
    document_id = await upload(account, storage)

    assert await visible(colleague(account), document_id) is False


async def test_the_uploader_sees_their_own_unfiled_document(
    account: Account, storage: DocumentStorage
) -> None:
    """Otherwise quarantine would be a regression rather than a fix: the row would vanish
    from Documents and the upload screen's status poll would start reporting "not found" for
    every file anyone sent."""
    document_id = await upload(account, storage)

    assert await visible(reading(account), document_id) is True


async def test_an_administrator_sees_what_is_waiting(
    account: Account, storage: DocumentStorage
) -> None:
    """Somebody has to be able to file it, and if the classifier fails that somebody is a
    person."""
    document_id = await upload(account, storage)
    admin = TenantContext.for_tenant(account.tenant_id, (account.quarantine_label,))

    assert await visible(admin, document_id) is True


# --- the four things the exception must not do ----------------------------------------


async def test_the_exception_expires_when_the_document_is_ready(
    account: Account, storage: DocumentStorage
) -> None:
    """`status <> 'ready'` bounds it to the ingestion window instead of granting a permanent
    second route to a document.

    Once the document is filed, labels answer and nothing else does — so an uploader later
    removed from a compartment loses the document they put there, which is what a compartment
    means. Without the bound, uploading would be a way to keep access forever.
    """
    document_id = await upload(account, storage)
    assert await visible(reading(account), document_id) is True

    await mark_ready(document_id)

    assert await visible(reading(account), document_id) is False


async def test_the_exception_does_not_reach_another_persons_document(
    account: Account, storage: DocumentStorage
) -> None:
    """It is keyed on `uploaded_by`, so a context that merely *sets* a user id gains nothing.

    Worth its own test because the clause is a disjunct: an error that compared the column to
    something always-true — or omitted the comparison — would open every in-flight document in
    the tenant, and every other assertion here would still pass.
    """
    document_id = await upload(account, storage)
    someone_else = TenantContext.for_tenant(account.tenant_id, (), user_id=account.admin_id)

    assert await visible(someone_else, document_id) is False


async def test_a_context_with_no_user_gains_nothing(
    account: Account, storage: DocumentStorage
) -> None:
    """An unbound `zenith.user_id` is NULL, and `uploaded_by = NULL` is never true. A session
    that forgets to bind it reads less, never more — the direction every policy here fails
    in."""
    document_id = await upload(account, storage)
    anonymous = TenantContext.for_tenant(account.tenant_id, (account.default_label,))

    assert await visible(anonymous, document_id) is False


async def test_the_exception_does_not_cross_tenants(
    account: Account, storage: DocumentStorage
) -> None:
    """`tenant_id = zenith_current_tenant()` is a conjunct sitting outside the parenthesised
    disjunction, and this is the test that it stayed there. Moved inside by a stray bracket,
    an uploader id would reach across organisations."""
    document_id = await upload(account, storage)
    elsewhere = TenantContext.for_tenant(uuid4(), (), user_id=account.member_id)

    assert await visible(elsewhere, document_id) is False


async def test_the_exception_does_not_reach_the_chunks(
    account: Account, storage: DocumentStorage
) -> None:
    """It exists so somebody can track a file they are uploading. It is not a way into the
    search index, so `chunks` keeps its label-only policy and the uploader sees none of them
    while the document waits."""
    document_id = await upload(account, storage)
    async with owner_session() as session:
        await session.execute(
            text(
                "INSERT INTO chunks (tenant_id, document_id, page_num, text, "
                "  char_start, char_end) "
                "VALUES (:t, :d, 1, 'a passage', 0, 9)"
            ),
            {"t": account.tenant_id, "d": document_id},
        )

    async with tenant_session(reading(account)) as session:
        found = await session.scalar(
            text("SELECT count(*) FROM chunks WHERE document_id = :d"), {"d": document_id}
        )

    assert found == 0


async def test_the_exception_is_read_only(account: Account, storage: DocumentStorage) -> None:
    """`USING` carries the exception; `WITH CHECK` keeps 0001's label rule untouched.

    So an uploader may *read* their quarantined document and may not write a row into a
    compartment they do not hold. `DocumentService` refuses that too, but the database must
    not be the layer that relies on it.
    """
    document_id = await upload(account, storage)

    with pytest.raises(DBAPIError) as refused:
        async with tenant_session(reading(account)) as session:
            await session.execute(
                text("UPDATE documents SET filename = 'renamed.pdf' WHERE id = :d"),
                {"d": document_id},
            )

    assert "row-level security" in str(refused.value).lower()


async def test_the_uploader_keeps_sight_of_it_while_it_is_being_filed(
    account: Account, storage: DocumentStorage
) -> None:
    """The window the exception was written for, and the one it originally missed.

    `_persist` wrote `status = 'ready'` in the same transaction as the chunks, *before* the
    classifier is asked where the document belongs. The exception is `status <> 'ready'`, so
    it was already switched off during the single step it exists to cover: a member who
    uploaded a file lost it from Documents while a model was being asked about it, and the
    upload screen's poller started reporting "not found". A filing that then failed left it
    admin-only for good.

    Migration 0019 puts `classifying` between `embedding` and `ready`, so the exception covers
    the model call. This asserts the property rather than the migration: at every status that
    is not `ready`, the uploader can still see their own document.
    """
    document_id = await upload(account, storage)

    for status in ("pending", "parsing", "chunking", "embedding", "classifying"):
        async with owner_session() as session:
            await session.execute(
                text("UPDATE documents SET status = :s WHERE id = :d"),
                {"s": status, "d": document_id},
            )

        assert await visible(reading(account), document_id) is True, (
            f"the uploader lost their own document at {status!r}"
        )
        # And nobody else did, at any of them.
        assert await visible(colleague(account), document_id) is False
