"""Upload, deduplication and deletion, against a real Postgres with RLS active.

The interesting cases here are not "does it store a file". They are the ones where the
answer decides who can read a document afterwards.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.common.exceptions import (
    LimitExceededError,
    NotFoundError,
    PermissionDeniedError,
    UnsupportedFileError,
)
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


async def labels_of(tenant_id: UUID, document_id: UUID) -> set[UUID]:
    """Read the denormalised copy — the one RLS actually consults.

    Checking the join table would confirm the write and say nothing about visibility. The
    array is what decides who sees the document, so it is the array these tests assert on.
    """
    async with owner_session() as session:
        row = await session.scalar(
            text("SELECT label_ids FROM documents WHERE id = :d"), {"d": document_id}
        )
    return set(row or [])


async def profile_for(
    account: Account, *, admin: bool = True, labels: tuple[UUID, ...] | None = None
) -> AccessProfile:
    from app.features.auth.permissions import CATALOGUE, SYSTEM_ROLES

    async with owner_session() as session:
        reachable = tuple(
            await session.scalars(
                text(
                    "SELECT rl.label_id FROM role_labels rl "
                    "JOIN user_roles ur ON ur.role_id = rl.role_id WHERE ur.user_id = :u"
                ),
                {"u": account.admin_id if admin else account.member_id},
            )
        )
    return AccessProfile(
        user_id=account.admin_id if admin else account.member_id,
        context=TenantContext.for_tenant(
            account.tenant_id, labels if labels is not None else reachable
        ),
        permissions=frozenset(CATALOGUE if admin else SYSTEM_ROLES["member"]),
    )


async def test_a_new_tenant_has_a_default_label_its_system_roles_reach(account: Account) -> None:
    """Without this, an upload with no label has nowhere to go.

    Both halves matter. A missing default means the upload is refused; a default no role
    reaches means the document is stored invisible to everyone, including whoever uploaded
    it, and no error is raised at all.
    """
    async with owner_session() as session:
        default = await session.scalar(
            text("SELECT id FROM access_labels WHERE tenant_id = :t AND is_default"),
            {"t": account.tenant_id},
        )
        reaching = await session.scalar(
            text("SELECT count(*) FROM role_labels WHERE label_id = :l"), {"l": default}
        )

    assert default is not None
    assert reaching == 2, "the default label must be reachable by both system roles"


async def test_upload_stores_a_row_a_file_and_a_label(
    account: Account, storage: DocumentStorage
) -> None:
    profile = await profile_for(account)

    result = await DocumentService(profile, storage).upload("report.pdf", pdf())

    assert result.deduplicated is False
    assert result.document.status == "pending"
    assert result.document.size_bytes == len(PDF)
    assert storage.path_for(account.tenant_id, result.document.sha256).exists()
    assert await labels_of(account.tenant_id, result.document.id) == set(result.labels)


async def test_a_document_is_never_stored_without_a_label(
    account: Account, storage: DocumentStorage
) -> None:
    """The hole this whole design exists to close.

    An empty `label_ids` satisfies the policy clause `label_ids = '{}' OR ...`, so a
    document that arrives unlabelled is readable by every user in the tenant. There would
    be no error, no failed status and nothing in a log — only a Finance contract in
    everyone's search results.
    """
    profile = await profile_for(account)

    result = await DocumentService(profile, storage).upload("unclassified.pdf", pdf())

    assert await labels_of(account.tenant_id, result.document.id) != set()


async def test_the_same_file_twice_is_one_row_and_one_file(
    account: Account, storage: DocumentStorage
) -> None:
    service = DocumentService(await profile_for(account), storage)

    first = await service.upload("report.pdf", pdf())
    second = await service.upload("report-copy.pdf", pdf())

    assert second.deduplicated is True
    assert second.document.id == first.document.id
    assert len(list((storage.root / str(account.tenant_id)).iterdir())) == 1


async def test_deduplication_unions_the_labels(account: Account, storage: DocumentStorage) -> None:
    """Approved deliberately: the second uploader already holds the bytes.

    Storing a second copy would cost the disk and, worse, put duplicate chunks into every
    later search result. The union is visible in the response so the widening is something
    the uploader can see rather than something that happens to them.
    """
    async with owner_session() as session:
        default = await session.scalar(
            text("SELECT id FROM access_labels WHERE tenant_id = :t AND is_default"),
            {"t": account.tenant_id},
        )
    admin = await profile_for(account)

    first = await DocumentService(admin, storage).upload("report.pdf", pdf(), [default])
    second = await DocumentService(admin, storage).upload(
        "report.pdf", pdf(), [account.finance_label]
    )

    assert second.deduplicated is True
    assert set(second.labels) == {default, account.finance_label}
    assert await labels_of(account.tenant_id, first.document.id) == {
        default,
        account.finance_label,
    }


async def test_a_label_the_caller_does_not_reach_is_refused(
    account: Account, storage: DocumentStorage
) -> None:
    """Filing into a compartment you are locked out of.

    It would let someone place a document where they cannot see it — and therefore cannot
    be shown to have seen it — which is an audit problem before it is an access one.
    """
    profile = await profile_for(account, labels=())

    with pytest.raises(PermissionDeniedError):
        await DocumentService(profile, storage).upload("report.pdf", pdf(), [account.finance_label])


async def test_an_unknown_label_is_refused(account: Account, storage: DocumentStorage) -> None:
    profile = await profile_for(account)

    with pytest.raises(NotFoundError):
        await DocumentService(profile, storage).upload("report.pdf", pdf(), [uuid4()])


async def test_a_file_that_is_not_a_pdf_is_rejected_before_anything_is_stored(
    account: Account, storage: DocumentStorage
) -> None:
    """Judged on the bytes. The declared content type is a claim the client makes."""
    profile = await profile_for(account)

    with pytest.raises(UnsupportedFileError):
        await DocumentService(profile, storage).upload("virus.pdf", pdf(b"MZ\x90\x00 an exe"))

    assert not storage.staging.exists() or list(storage.staging.iterdir()) == []
    async with tenant_session(profile.context) as session:
        assert await session.scalar(text("SELECT count(*) FROM documents")) == 0


async def test_an_oversized_file_is_rejected(
    account: Account, storage: DocumentStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "max_file_bytes", 8)
    profile = await profile_for(account)

    with pytest.raises(LimitExceededError):
        await DocumentService(profile, storage).upload("huge.pdf", pdf())


async def test_a_failed_write_leaves_no_file_behind(
    account: Account, storage: DocumentStorage
) -> None:
    """The staged file must not outlive the transaction that rejected it.

    Otherwise every refused upload — wrong label, over quota, unknown label — leaks a
    copy of a customer document onto the disk, outside the storage layout and outside
    anything that knows how to delete it.
    """
    profile = await profile_for(account)

    with pytest.raises(NotFoundError):
        await DocumentService(profile, storage).upload("report.pdf", pdf(), [uuid4()])

    assert list(storage.staging.iterdir()) == []


async def test_deletion_removes_the_row_and_the_file(
    account: Account, storage: DocumentStorage
) -> None:
    profile = await profile_for(account)
    service = DocumentService(profile, storage)
    result = await service.upload("report.pdf", pdf())
    path = storage.path_for(account.tenant_id, result.document.sha256)

    await service.delete(result.document.id)

    assert not path.exists()
    async with tenant_session(profile.context) as session:
        assert await session.scalar(text("SELECT count(*) FROM documents")) == 0


async def test_delete_own_cannot_reach_another_users_upload(
    account: Account, storage: DocumentStorage
) -> None:
    admin = await profile_for(account)
    uploaded = await DocumentService(admin, storage).upload("report.pdf", pdf())

    from app.features.auth.permissions import CATALOGUE

    member = AccessProfile(
        user_id=account.member_id,
        context=admin.context,
        permissions=frozenset({"documents.delete.own"}),
    )
    assert "documents.delete.own" in CATALOGUE

    with pytest.raises(PermissionDeniedError):
        await DocumentService(member, storage).delete(uploaded.document.id)

    assert storage.path_for(account.tenant_id, uploaded.document.sha256).exists()


async def test_another_tenant_cannot_see_or_delete_the_document(
    account: Account, storage: DocumentStorage
) -> None:
    """Through RLS, with the other tenant's own context — not with a filter in the query.

    A 404 rather than a 403, because "forbidden" would confirm the document exists.
    """
    profile = await profile_for(account)
    uploaded = await DocumentService(profile, storage).upload("report.pdf", pdf())

    intruder = AccessProfile(
        user_id=uuid4(),
        context=TenantContext.for_tenant(uuid4()),
        permissions=frozenset({"documents.delete.any"}),
    )

    visible, _ = await DocumentService(intruder, storage).page()
    assert visible == []
    with pytest.raises(NotFoundError):
        await DocumentService(intruder, storage).delete(uploaded.document.id)
