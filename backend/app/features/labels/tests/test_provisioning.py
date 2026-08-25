"""The label every tenant must have, and what happens when it is gone.

A tenant is provisioned with a default label and there is no supported way to remove it —
and one was found without it, because the label management screen deletes any label
including that one. The consequence is not subtle: every upload that names no label is
refused, which is the product's most common action, for everybody in the tenant.
"""

from pathlib import Path

from app.features.labels.provisioning import DEFAULT_LABEL
from conftest import Account


async def test_a_tenant_that_lost_its_default_gets_it_back(account: Account) -> None:
    """The lockout this repairs is total: `DocumentService` refuses every upload that names
    no label, so a deleted default stops the product's most common action for everybody in
    the tenant."""
    from sqlalchemy import text

    from app.core.database import owner_session
    from app.features.labels.provisioning import ensure_default_label

    async with owner_session() as session:
        await session.execute(
            text("DELETE FROM access_labels WHERE tenant_id = :t AND is_default"),
            {"t": account.tenant_id},
        )
        restored = await ensure_default_label(session, account.tenant_id)

    assert restored.is_default
    assert restored.name == DEFAULT_LABEL


async def test_restoring_grants_it_to_the_system_roles_and_no_others(account: Account) -> None:
    """The property that makes repairing safer than refusing.

    Exactly what provisioning would have created, so no document becomes readable by
    anybody who could not have read an unclassified upload the day the tenant was made. A
    role an administrator added later reaches it when they say so.
    """
    from sqlalchemy import text

    from app.core.database import owner_session
    from app.features.labels.provisioning import ensure_default_label

    async with owner_session() as session:
        await session.execute(
            text("INSERT INTO roles (tenant_id, name, is_system) VALUES (:t, 'Later', false)"),
            {"t": account.tenant_id},
        )
        await session.execute(
            text("DELETE FROM access_labels WHERE tenant_id = :t AND is_default"),
            {"t": account.tenant_id},
        )
        restored = await ensure_default_label(session, account.tenant_id)
        granted = list(
            await session.scalars(
                text(
                    "SELECT r.name FROM role_labels rl JOIN roles r ON r.id = rl.role_id "
                    "WHERE rl.label_id = :l"
                ),
                {"l": restored.id},
            )
        )

    assert "Later" not in granted
    assert set(granted) == {"admin", "member"}


async def test_calling_it_twice_returns_the_same_label(account: Account) -> None:
    """Idempotent, because the upload path calls it whenever it finds none — and two
    uploads arriving together both will."""
    from app.core.database import owner_session
    from app.features.labels.provisioning import ensure_default_label

    async with owner_session() as session:
        first = await ensure_default_label(session, account.tenant_id)
        second = await ensure_default_label(session, account.tenant_id)

    assert first.id == second.id


async def test_an_upload_with_no_label_survives_the_default_being_deleted(
    account: Account, tmp_path: Path
) -> None:
    """End to end: the refusal is gone and the upload still lands somewhere narrow.

    Since 0017 an upload that names no compartment waits in the *quarantine* label rather
    than the default, so deleting the default no longer touches this path — which is a
    stronger version of what this test was always about. The default is still needed later,
    by the classifier, when it declines and has to release the document; a tenant missing one
    at that point keeps the document quarantined with a sentence saying why, which leaves one
    document needing an administrator rather than the whole product refusing uploads.
    """
    from sqlalchemy import text

    from app.core.database import owner_session
    from app.features.documents.service import DocumentService
    from app.features.documents.storage import DocumentStorage
    from app.features.documents.tests.test_upload import pdf, profile_for

    async with owner_session() as session:
        await session.execute(
            text("DELETE FROM access_labels WHERE tenant_id = :t AND is_default"),
            {"t": account.tenant_id},
        )

    profile = await profile_for(account)
    storage = DocumentStorage(root=tmp_path)
    result = await DocumentService(profile, storage).upload("unclassified.pdf", pdf())

    # Quarantined, and the deleted default is *not* quietly recreated: nothing on this path
    # needs it any more, and recreating a label somebody deleted belongs where it is actually
    # required rather than as a side effect of an unrelated upload.
    async with owner_session() as session:
        present = (
            await session.execute(
                text(
                    "SELECT id, name, is_default, is_quarantine "
                    "FROM access_labels WHERE tenant_id = :t"
                ),
                {"t": account.tenant_id},
            )
        ).all()

    assert [row for row in present if row.is_default] == []
    quarantine = [row for row in present if row.is_quarantine]
    assert len(quarantine) == 1
    assert result.labels == [quarantine[0].id]
