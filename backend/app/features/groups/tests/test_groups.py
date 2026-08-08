"""Group administration, and the tenant boundary it must not cross."""

from uuid import uuid4

import pytest

from app.common.exceptions import ConflictError, NotFoundError
from app.features.auth.permissions import CATALOGUE
from app.features.auth.service import AccessProfile
from app.features.groups.service import GroupService
from app.features.tenancy.context import TenantContext
from conftest import Account


def admin(account: Account) -> AccessProfile:
    return AccessProfile(
        user_id=account.admin_id,
        context=TenantContext.for_tenant(account.tenant_id, [account.default_label]),
        permissions=frozenset(CATALOGUE),
    )


async def test_a_group_starts_empty_and_opens_nothing(account: Account) -> None:
    """Creating a group is not granting anything, and the read model says so."""
    group = await GroupService(admin(account)).create("Engineering", "Builds the product")

    assert group.name == "Engineering"
    assert group.members == 0
    assert group.label_ids == []


async def test_two_groups_cannot_share_a_name(account: Account) -> None:
    service = GroupService(admin(account))
    await service.create("Finance")

    with pytest.raises(ConflictError):
        await service.create("Finance")


async def test_mapping_labels_replaces_rather_than_adds(account: Account) -> None:
    """A caller sending the full set knows what the group opens afterwards. A patch would
    make the result depend on state they did not read."""
    service = GroupService(admin(account))
    group = await service.create("Finance")

    await service.set_labels(group.id, [account.finance_label, account.default_label])
    narrowed = await service.set_labels(group.id, [account.finance_label])

    assert narrowed.label_ids == [account.finance_label]


async def test_a_label_from_another_tenant_is_absent_rather_than_forbidden(
    account: Account,
) -> None:
    """404, not 403. Telling the caller a label exists but is not theirs confirms it
    exists, which is the inference mvp.md 3.1 forbids."""
    service = GroupService(admin(account))
    group = await service.create("Finance")

    with pytest.raises(NotFoundError):
        await service.set_labels(group.id, [uuid4()])


async def test_membership_is_reported_from_both_sides(account: Account) -> None:
    """`PUT /groups/{id}/members` and `PUT /users/{id}/groups` write the same relation, and
    both screens have to agree about it."""
    service = GroupService(admin(account))
    group = await service.create("Finance")

    populated = await service.set_members(group.id, [account.member_id])

    assert populated.members == 1
    assert await service.of_user(account.member_id) == [group.id]


async def test_removing_someone_from_a_group_takes_the_access_with_them(
    account: Account,
) -> None:
    service = GroupService(admin(account))
    group = await service.create("Finance")
    await service.set_members(group.id, [account.member_id])

    await service.set_members(group.id, [])

    assert await service.of_user(account.member_id) == []


async def test_deleting_a_group_removes_what_it_opened(account: Account) -> None:
    """The cascade is the behaviour, not an implementation detail: deleting a group takes
    away the access it conferred rather than transferring it anywhere."""
    service = GroupService(admin(account))
    group = await service.create("Finance")
    await service.set_labels(group.id, [account.finance_label])
    await service.set_members(group.id, [account.member_id])

    await service.delete(group.id)

    assert await service.of_user(account.member_id) == []
    assert group.id not in {existing.id for existing in await service.visible()}


async def test_a_group_in_another_tenant_does_not_exist_from_here(account: Account) -> None:
    service = GroupService(admin(account))

    with pytest.raises(NotFoundError):
        await service.delete(uuid4())
