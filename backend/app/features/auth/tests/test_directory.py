"""The staff list, and the aggregate that would quietly lie about it."""

from uuid import uuid4

from sqlalchemy import text

from app.core.database import owner_session
from app.features.auth.access.permissions import CATALOGUE
from app.features.auth.directory import DirectoryService
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


async def test_everyone_in_the_tenant_is_listed(account: Account) -> None:
    members = await DirectoryService(admin(account)).members()

    assert {member.email for member in members} == {account.admin_email, account.member_email}


async def test_a_member_carries_the_groups_they_are_in(account: Account) -> None:
    groups = GroupService(admin(account))
    finance = await groups.create("Finance")
    await groups.set_members(finance.id, [account.member_id])

    members = {member.id: member for member in await DirectoryService(admin(account)).members()}

    assert members[account.member_id].group_ids == [finance.id]
    assert members[account.admin_id].group_ids == []


async def test_holding_two_roles_and_two_groups_reports_two_of_each(account: Account) -> None:
    """The DISTINCT that keeps this honest.

    Two left joins multiply each other: without it, a user in two groups holding two roles
    comes back with four of each, and the screen renders a duplicated list that looks like
    the access was granted twice.
    """
    groups = GroupService(admin(account))
    first = await groups.create("Finance")
    second = await groups.create("Legal")
    await groups.set_members(first.id, [account.member_id])
    await groups.set_members(second.id, [account.member_id])

    async with owner_session() as session:
        extra = await session.scalar(
            text("INSERT INTO roles (tenant_id, name) VALUES (:t, :n) RETURNING id"),
            {"t": account.tenant_id, "n": f"extra {uuid4()}"},
        )
        await session.execute(
            text("INSERT INTO user_roles (user_id, role_id) VALUES (:u, :r)"),
            {"u": account.member_id, "r": extra},
        )

    members = {member.id: member for member in await DirectoryService(admin(account)).members()}
    member = members[account.member_id]

    assert len(member.group_ids) == 2
    assert len(member.role_ids) == 2


async def test_somebody_with_no_roles_and_no_groups_is_still_listed(account: Account) -> None:
    """A user who can sign in and do nothing is a real intermediate state — somebody being
    onboarded, or moved between teams — and they are exactly who an administrator opened
    this screen to fix."""
    async with owner_session() as session:
        await session.execute(
            text("INSERT INTO users (tenant_id, email, password_hash) VALUES (:t, :e, 'x')"),
            {"t": account.tenant_id, "e": f"new-{uuid4()}@example.com"},
        )

    members = await DirectoryService(admin(account)).members()
    unassigned = [m for m in members if not m.role_ids and not m.group_ids]

    assert len(unassigned) == 1


async def test_another_tenants_staff_are_absent_rather_than_filtered(account: Account) -> None:
    """RLS is what makes this true, not a WHERE clause somebody has to remember."""
    from app.features.tenancy.service import TenantService

    other = await TenantService().create(f"Other {uuid4()}")
    async with owner_session() as session:
        await session.execute(
            text("INSERT INTO users (tenant_id, email, password_hash) VALUES (:t, :e, 'x')"),
            {"t": other.id, "e": f"outsider-{uuid4()}@example.com"},
        )

    emails = {member.email for member in await DirectoryService(admin(account)).members()}

    assert not any("outsider" in email for email in emails)
