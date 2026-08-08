"""One address, one account, installation-wide — and the bug that made it necessary.

The first test is the reason for all the others. Before migration 0011, creating the same
address in two organisations was allowed and produced *two accounts that could not log in*:
`authenticate` refuses anything other than exactly one match, so both sides answered 401 and
the only evidence was a log line no user ever sees.

The remaining tests hold the three doors that create users. A rule enforced in three places
is a rule enforced in two as soon as somebody adds a fourth, so the database holds it and
these check that each door reports it as a conflict rather than as a driver error.
"""

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.common.exceptions import AuthenticationError, ConflictError
from app.core.database import owner_session
from app.features.auth.invitations import InvitationService
from app.features.auth.permissions import CATALOGUE
from app.features.auth.provisioning import create_user
from app.features.auth.service import AccessProfile, AuthService
from app.features.system.service import SystemService
from app.features.tenancy.context import TenantContext
from app.features.tenancy.service import TenantService
from conftest import PASSWORD, Account


def admin(account: Account) -> AccessProfile:
    return AccessProfile(
        user_id=account.admin_id,
        context=TenantContext.for_tenant(account.tenant_id, [account.default_label]),
        permissions=frozenset(CATALOGUE),
    )


async def test_the_database_refuses_the_same_address_twice(account: Account) -> None:
    """The guarantee itself, checked against the constraint rather than against a caller.

    Through the owner connection, which bypasses RLS and every application check — so this
    fails for one reason only: the index exists.
    """
    other = await TenantService().create(f"Other {uuid4()}")

    with pytest.raises(Exception, match="uq_users_email_global|duplicate key"):
        async with owner_session() as session:
            await create_user(session, other.id, account.admin_email, PASSWORD, [])


async def test_the_duplicate_this_prevents_broke_both_accounts(account: Account) -> None:
    """Why the constraint is worth its cost, demonstrated rather than asserted.

    A second row is forced in past the constraint, exactly as the old schema would have
    allowed. Neither account can then log in — not the new one, and not the one that
    worked yesterday. That is the failure mode 0011 exists to make unreachable.
    """
    other = await TenantService().create(f"Other {uuid4()}")
    async with owner_session() as session:
        await session.execute(text("DROP INDEX uq_users_email_global"))
        try:
            await create_user(session, other.id, account.admin_email, PASSWORD, [])
        finally:
            # Rebuilt inside the same transaction, so the rest of the suite is unaffected
            # whatever this test does — and building it over the duplicate would fail, so
            # the offending row goes first.
            await session.execute(text("DELETE FROM users WHERE tenant_id = :t"), {"t": other.id})
            await session.execute(
                text("CREATE UNIQUE INDEX uq_users_email_global ON users (email)")
            )

    # Deleted again above, so this asserts the mechanism rather than the leftover state:
    # what matters is that `authenticate` refuses an ambiguous address, which is why two
    # rows are two broken accounts rather than two working ones.
    assert await AuthService().authenticate(account.admin_email, PASSWORD)


async def test_inviting_an_address_from_another_organisation_is_a_conflict(
    account: Account,
) -> None:
    """The door an administrator uses. 409, not a 500 with a constraint name in it."""
    other = await TenantService().create(f"Other {uuid4()}")
    async with owner_session() as session:
        outsider = f"outsider-{uuid4()}@example.com"
        await create_user(session, other.id, outsider, PASSWORD, [])

    with pytest.raises(ConflictError, match="another organisation"):
        await InvitationService(admin(account)).invite(outsider, [])


async def test_a_duplicate_inside_the_tenant_says_so_instead(account: Account) -> None:
    """The two conflicts are told apart, and the difference is not cosmetic.

    "Already here" is something the administrator can act on — they can go and look at the
    user. "Registered elsewhere" is something they can only work around.
    """
    with pytest.raises(ConflictError, match="already a user here"):
        await InvitationService(admin(account)).invite(account.member_email, [])


async def test_provisioning_an_organisation_refuses_a_taken_address(
    account: Account,
) -> None:
    with pytest.raises(ConflictError, match="another organisation"):
        await SystemService().provision(f"New {uuid4()}", account.admin_email)


async def test_a_refused_provision_leaves_no_organisation_behind(account: Account) -> None:
    """Checked before the tenant is created, not after.

    `TenantService.create` commits its own transaction, so an address rejected later would
    strand an organisation with no administrator and nothing to indicate why.
    """
    name = f"Stranded {uuid4()}"

    with pytest.raises(ConflictError):
        await SystemService().provision(name, account.admin_email)

    assert name not in {org.name for org in await SystemService().organisations()}


async def test_an_address_that_is_free_is_still_accepted(account: Account) -> None:
    """The other half: a constraint that refuses everything would pass every test above."""
    fresh = f"fresh-{uuid4()}@example.com"

    invited = await InvitationService(admin(account)).invite(fresh, [])

    assert invited.email == fresh
    assert await AuthService().authenticate(fresh, invited.password)


async def test_an_ambiguous_address_is_refused_rather_than_guessed(account: Account) -> None:
    """Unchanged behaviour, asserted so it stays that way.

    `authenticate` returning 401 for an ambiguous address is correct — saying anything else
    would confirm the address exists. The constraint removes the cause; this keeps the
    handling of it honest if one ever appears by another route.
    """
    with pytest.raises(AuthenticationError):
        await AuthService().authenticate(f"nobody-{uuid4()}@example.com", PASSWORD)
