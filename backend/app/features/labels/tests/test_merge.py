"""Merging labels, and the access it moves while doing it.

A merge looks like tidying up a duplicate tag. It is not: labels are the access-control
primitive, so folding two together hands documents between roles in two directions at
once, and only one of them is the one anybody thinks about.

*Forwards* — the documents move. Everything carrying the source ends up carrying the
target, so every role holding the target picks those documents up.

*Backwards* — the roles move. Every role holding the source ends up holding the target,
so it picks up everything that already carried the target. Nothing about those documents
changed; the role's reach did.

Both are tested here, separately, because a count that only walked the relabelled
documents would report a confident zero for the second one — a merge that silently hands
a role an entire compartment, reported as safe.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.common.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.core.database import owner_session
from app.features.auth.model import Role
from app.features.labels.model import AccessLabel, RoleLabel
from app.features.labels.service import LabelService
from app.features.tenancy.context import TenantContext
from conftest import Account

pytestmark = pytest.mark.asyncio


async def label(tenant_id: UUID, name: str) -> UUID:
    async with owner_session() as session:
        created = AccessLabel(tenant_id=tenant_id, name=name)
        session.add(created)
        await session.flush()
        return created.id


async def role_reaching(tenant_id: UUID, *label_ids: UUID) -> UUID:
    async with owner_session() as session:
        created = Role(tenant_id=tenant_id, name=f"role-{uuid4()}")
        session.add(created)
        await session.flush()
        session.add_all(RoleLabel(role_id=created.id, label_id=label_id) for label_id in label_ids)
        return created.id


async def document(tenant_id: UUID, label_ids: list[UUID], name: str = "doc.pdf") -> UUID:
    async with owner_session() as session:
        document_id = await session.scalar(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes) "
                "VALUES (:t, :name, :sha, 10) RETURNING id"
            ),
            {"t": tenant_id, "name": name, "sha": uuid4().hex + uuid4().hex[:32]},
        )
        for label_id in label_ids:
            await session.execute(
                text("INSERT INTO document_labels (document_id, label_id) VALUES (:d, :l)"),
                {"d": document_id, "l": label_id},
            )
    return UUID(str(document_id))


async def labels_of(document_id: UUID) -> set[UUID]:
    """Read `documents.label_ids`, the copy RLS actually enforces on — not
    `document_labels`. A merge that updated the source and left the copy stale would pass
    a test that read the source."""
    async with owner_session() as session:
        row = await session.scalar(
            text("SELECT label_ids FROM documents WHERE id = :id"), {"id": document_id}
        )
        return set(row or [])


async def label_exists(label_id: UUID) -> bool:
    async with owner_session() as session:
        return (
            await session.scalar(
                text("SELECT 1 FROM access_labels WHERE id = :id"), {"id": label_id}
            )
        ) is not None


def everything(account: Account, *extra: UUID) -> TenantContext:
    """A context reaching every label involved — what `merge` requires, and what an
    administrator running a cleanup would actually hold."""
    return TenantContext.for_tenant(
        account.tenant_id, [account.default_label, account.finance_label, *extra]
    )


# --- what it moves ------------------------------------------------------------------


async def test_documents_carry_the_target_afterwards(account: Account) -> None:
    duplicate = await label(account.tenant_id, "Finanace")  # the typo being cleaned up
    moved = await document(account.tenant_id, [duplicate])

    await LabelService(everything(account, duplicate)).merge(
        [duplicate], account.finance_label, acknowledge_widening=True
    )

    assert await labels_of(moved) == {account.finance_label}
    assert not await label_exists(duplicate), "the source label should be gone"


async def test_a_document_carrying_both_survives_the_move(account: Account) -> None:
    """The collision the composite primary key would otherwise reject.

    Without `ON CONFLICT DO NOTHING` the insert fails on this row, the whole merge rolls
    back, and the caller is told about a constraint they have no way to act on.
    """
    duplicate = await label(account.tenant_id, "Finanace")
    both = await document(account.tenant_id, [duplicate, account.finance_label])

    await LabelService(everything(account, duplicate)).merge(
        [duplicate], account.finance_label, acknowledge_widening=True
    )

    assert await labels_of(both) == {account.finance_label}


async def test_roles_reaching_the_source_reach_the_target(account: Account) -> None:
    """The half that is easy to leave out. Reassigning only the documents would strip a
    role of everything it was admitted to, which reads as data loss to the people who had
    access yesterday."""
    duplicate = await label(account.tenant_id, "Finanace")
    orphaned = await role_reaching(account.tenant_id, duplicate)

    await LabelService(everything(account, duplicate)).merge(
        [duplicate], account.finance_label, acknowledge_widening=True
    )

    async with owner_session() as session:
        reached = set(
            await session.scalars(
                text("SELECT label_id FROM role_labels WHERE role_id = :r"), {"r": orphaned}
            )
        )
    assert reached == {account.finance_label}


async def test_several_sources_fold_into_one_target(account: Account) -> None:
    first = await label(account.tenant_id, "Finanace")
    second = await label(account.tenant_id, "finance ")
    one = await document(account.tenant_id, [first], name="a.pdf")
    two = await document(account.tenant_id, [second], name="b.pdf")

    result = await LabelService(everything(account, first, second)).merge(
        [first, second], account.finance_label, acknowledge_widening=True
    )

    assert result.documents_relabelled == 2
    assert await labels_of(one) == {account.finance_label}
    assert await labels_of(two) == {account.finance_label}


# --- what it exposes ----------------------------------------------------------------


async def test_widening_counts_documents_the_merge_hands_to_a_new_role(
    account: Account,
) -> None:
    """Forwards: the Legal-only document that ends up visible to everyone.

    `restricted` is reachable by nobody but the caller; `wide` is reachable by an extra
    role. Merging restricted into wide gives that role a document it could not see.
    """
    restricted = await label(account.tenant_id, "Restricted")
    wide = await label(account.tenant_id, "Wide")
    await role_reaching(account.tenant_id, wide)
    await document(account.tenant_id, [restricted], name="secret.pdf")

    result = await LabelService(everything(account, restricted, wide)).merge(
        [restricted], wide, dry_run=True
    )

    assert result.visibility_widening == 1
    assert result.documents_relabelled == 1


async def test_widening_counts_documents_a_role_reaches_only_because_it_moved(
    account: Account,
) -> None:
    """Backwards, and the reason `_widening` walks roles rather than only documents.

    Nothing about `already.pdf` changes — it carries the target before and after. What
    changes is that the role holding only the source now holds the target, so it picks the
    document up. A merge that counted relabelled documents would report zero here.
    """
    source = await label(account.tenant_id, "Source")
    target = await label(account.tenant_id, "Target")
    await role_reaching(account.tenant_id, source)
    await document(account.tenant_id, [target], name="already.pdf")

    result = await LabelService(everything(account, source, target)).merge(
        [source], target, dry_run=True
    )

    assert result.documents_relabelled == 0, "no document carried the source"
    assert result.visibility_widening == 1, "but a role gained one"


async def test_a_merge_that_exposes_nothing_reports_zero(account: Account) -> None:
    """The count has to be able to say "safe", or nobody will read it when it says
    otherwise. Both labels are reachable by exactly the same roles, so folding them
    together moves no document across any boundary."""
    first = await label(account.tenant_id, "Alpha")
    second = await label(account.tenant_id, "Beta")
    await role_reaching(account.tenant_id, first, second)
    await document(account.tenant_id, [first], name="a.pdf")
    await document(account.tenant_id, [second], name="b.pdf")

    result = await LabelService(everything(account, first, second)).merge(
        [first], second, dry_run=True
    )

    assert result.visibility_widening == 0
    assert result.documents_relabelled == 1


async def test_a_widening_merge_is_refused_without_acknowledgement(account: Account) -> None:
    """The guard, in the same shape `delete` already uses: the dangerous consequence is
    not the one the caller came here thinking about."""
    restricted = await label(account.tenant_id, "Restricted")
    wide = await label(account.tenant_id, "Wide")
    await role_reaching(account.tenant_id, wide)
    moved = await document(account.tenant_id, [restricted], name="secret.pdf")

    with pytest.raises(ConflictError, match="1 document"):
        await LabelService(everything(account, restricted, wide)).merge([restricted], wide)

    assert await labels_of(moved) == {restricted}, "the refusal must not have moved anything"


async def test_a_dry_run_changes_nothing(account: Account) -> None:
    duplicate = await label(account.tenant_id, "Finanace")
    moved = await document(account.tenant_id, [duplicate])

    result = await LabelService(everything(account, duplicate)).merge(
        [duplicate], account.finance_label, dry_run=True
    )

    assert result.dry_run
    assert result.documents_relabelled == 1, "it still reports what it would do"
    assert await labels_of(moved) == {duplicate}
    assert await label_exists(duplicate)


# --- what it refuses ----------------------------------------------------------------


async def test_a_label_cannot_be_merged_into_itself(account: Account) -> None:
    """It would delete the label at the end of its own reassignment, and the cascade would
    strip it from every document whose only label it was — leaving them empty, which means
    visible to the entire tenant. A typo should not be able to do that."""
    with pytest.raises(ConflictError, match="itself"):
        await LabelService(everything(account)).merge(
            [account.finance_label], account.finance_label
        )


async def test_merging_a_label_the_caller_cannot_reach_is_refused(account: Account) -> None:
    """Not because the permission is missing — it is checked at the router — but because
    the widening count is computed under RLS. A caller who cannot see the source's
    documents would be handed a reassuringly small number for a merge that exposes a
    compartment they were never admitted to."""
    unreachable = await label(account.tenant_id, "Unreachable")

    context = TenantContext.for_tenant(account.tenant_id, [account.finance_label])
    with pytest.raises(PermissionDeniedError, match="do not reach"):
        await LabelService(context).merge([unreachable], account.finance_label)


async def test_merging_an_unknown_label_says_which_one(account: Account) -> None:
    ghost = uuid4()

    with pytest.raises(NotFoundError, match=str(ghost)):
        await LabelService(everything(account)).merge([ghost], account.finance_label)
