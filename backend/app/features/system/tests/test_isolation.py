"""The line above every tenant, and the two ways somebody could cross it.

This panel is the only surface in the product that reads across customers, so it is the only
one where RLS — which normally makes the question unanswerable rather than merely refused —
is switched off. Everything that keeps it safe is in this file's assertions.

The first test is the one that matters most. A tenant's own administrator holds the entire
permission catalogue *and edits it themselves* from the roles screen; if system authority
were a permission code, that screen would be a route out of their own tenant. It is not a
permission, and this proves the difference is real rather than intended.
"""

from uuid import UUID

import pytest
from sqlalchemy import text

from app.common.exceptions import PermissionDeniedError
from app.core.database import owner_session, tenant_session
from app.features.auth.dependencies import requires_system_admin
from app.features.auth.permissions import CATALOGUE
from app.features.auth.service import AccessProfile, AuthService
from app.features.tenancy.context import TenantContext
from conftest import Account


def profile_for(account: Account, *, system: bool) -> AccessProfile:
    return AccessProfile(
        user_id=account.admin_id,
        context=TenantContext.for_tenant(account.tenant_id, []),
        permissions=frozenset(CATALOGUE),
        is_system_admin=system,
    )


async def make_system_admin(user_id: UUID) -> None:
    """Through the owner connection — which is the only way, and the point of the next
    test down."""
    async with owner_session() as session:
        await session.execute(
            text("UPDATE users SET is_system_admin = true WHERE id = :u"), {"u": user_id}
        )


async def test_a_tenant_administrator_holding_every_permission_is_refused(
    account: Account,
) -> None:
    """The assertion the whole design rests on.

    Full `CATALOGUE` — `roles.manage`, `users.manage`, everything the product can grant —
    and it does not reach the system panel. If this ever passes, the panel has become
    something a customer can grant themselves.
    """
    with pytest.raises(PermissionDeniedError):
        await requires_system_admin(profile_for(account, system=False))


async def test_the_flag_is_what_opens_it(account: Account) -> None:
    granted = await requires_system_admin(profile_for(account, system=True))

    assert granted.is_system_admin


async def test_the_application_connection_cannot_grant_the_flag(account: Account) -> None:
    """The invariant underneath the first test, enforced by Postgres rather than by code.

    Migration 0010 narrows `zenith_app`'s UPDATE grant on `users` to a column list that
    omits `is_system_admin`. So it is not that no endpoint happens to write it — it is that
    no endpoint *can*, including one written next year by somebody who never read this file.
    """
    async with tenant_session(TenantContext.for_tenant(account.tenant_id, [])) as session:
        with pytest.raises(Exception, match="permission denied|privilege"):
            await session.execute(
                text("UPDATE users SET is_system_admin = true WHERE id = :u"),
                {"u": account.admin_id},
            )


async def test_the_application_connection_can_still_write_the_columns_it_needs(
    account: Account,
) -> None:
    """The other half of that narrowing: it must not have broken ordinary use.

    `change_password` and `sign_out_everywhere` write `token_version`, and renaming writes
    `name`. A column-level grant that was too tight would break all three, and would do it
    at runtime rather than at deploy.
    """
    async with tenant_session(TenantContext.for_tenant(account.tenant_id, [])) as session:
        await session.execute(
            text("UPDATE users SET name = :n, token_version = token_version + 1 WHERE id = :u"),
            {"n": "Renamed", "u": account.admin_id},
        )


async def test_the_flag_is_read_per_request_rather_than_carried_in_the_token(
    account: Account,
) -> None:
    """Granting it takes effect on the next request, not at the next login.

    The alternative — a claim in the JWT — would mean revocation waits for an access token
    to expire, and this is the one authority where that lag is worst.
    """
    before = await AuthService().profile(account.admin_id, account.tenant_id)
    assert not before.is_system_admin

    await make_system_admin(account.admin_id)

    after = await AuthService().profile(account.admin_id, account.tenant_id)
    assert after.is_system_admin


async def test_a_system_admin_in_one_tenant_is_not_one_in_another(account: Account) -> None:
    """Sanity on the shape of the thing: the flag is on a *user*, and a user belongs to a
    tenant. It confers authority over the installation, not membership of every tenant —
    reaching another customer's documents still needs a context this never creates."""
    await make_system_admin(account.admin_id)
    other = await AuthService().profile(account.member_id, account.tenant_id)

    assert not other.is_system_admin
