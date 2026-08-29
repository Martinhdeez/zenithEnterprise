"""The M3 surfaces, and the refusals that make them safe.

Roles decide who may do what, so the interesting assertions are the ones where a mistake
either locks a customer out of their own installation or hands them a permission nothing
enforces.
"""

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.common.exceptions import ConflictError, NotFoundError
from app.core.database import owner_session
from app.features.auth.access.roles import RoleService
from app.features.auth.service import AccessProfile
from app.features.generation.connector.config_service import LlmConfigService
from app.features.tenancy.context import TenantContext
from conftest import Account


def admin(account: Account) -> AccessProfile:
    from app.features.auth.access.permissions import CATALOGUE

    return AccessProfile(
        user_id=account.admin_id,
        context=TenantContext.for_tenant(account.tenant_id, [account.default_label]),
        permissions=frozenset(CATALOGUE),
    )


# --- roles -------------------------------------------------------------------------


async def test_the_seeded_system_roles_are_listed(account: Account) -> None:
    roles = await RoleService(admin(account)).visible()

    names = {role.name for role in roles}
    assert {"admin", "member"} <= names
    assert all(role.is_system for role in roles if role.name in {"admin", "member"})


async def test_a_role_reports_how_many_users_hold_it(account: Account) -> None:
    """A role about to be edited is safer to reason about when you know it is in use."""
    roles = {role.name: role for role in await RoleService(admin(account)).visible()}

    assert roles["admin"].users == 1
    assert roles["member"].users == 1


async def test_an_unknown_permission_is_refused(account: Account) -> None:
    """A permission nobody checks is a lie in the administration screen: it appears
    granted and grants nothing."""
    with pytest.raises(NotFoundError, match="unknown permission"):
        await RoleService(admin(account)).create("auditor", ["documents.invent"])


async def test_a_role_can_be_created_and_its_permissions_replaced(account: Account) -> None:
    service = RoleService(admin(account))

    created = await service.create("auditor", ["query.history.any"])
    assert created.permissions == ["query.history.any"]

    updated = await service.set_permissions(created.id, ["query.execute", "query.history.own"])
    assert updated.permissions == ["query.execute", "query.history.own"]
    assert "query.history.any" not in updated.permissions, "replace, not merge"


async def test_a_system_role_cannot_be_edited(account: Account) -> None:
    """Letting a customer strip `admin` of `roles.manage` is the lockout this module
    refuses, reached by a longer route."""
    service = RoleService(admin(account))
    system = next(role for role in await service.visible() if role.name == "admin")

    with pytest.raises(ConflictError, match="system roles"):
        await service.set_permissions(system.id, ["query.execute"])


async def test_an_edit_that_orphans_the_tenant_is_refused(account: Account) -> None:
    """The assertion this module exists for.

    Recovering from "nobody holds roles.manage" on an on-premise install means somebody in
    `psql` on the customer's server. That is not a support call this product should ever
    generate, so the edit is rejected rather than warned about.
    """
    service = RoleService(admin(account))
    # A custom role that is the tenant's only administrator: the seeded `admin` role is
    # unassigned first, so nothing else holds the pair.
    keeper = await service.create("keeper", ["users.manage", "roles.manage"])
    await service.assign(account.admin_id, [keeper.id])

    with pytest.raises(ConflictError, match="leave nobody"):
        await service.set_permissions(keeper.id, ["query.execute"])


async def test_an_edit_is_allowed_when_another_held_role_still_administers(
    account: Account,
) -> None:
    """The check is about *held* roles. A role with the permissions and no users protects
    nobody, and one that still has users does."""
    service = RoleService(admin(account))
    spare = await service.create("second-admin", ["users.manage", "roles.manage"])
    await service.assign(account.member_id, [spare.id])
    doomed = await service.create("doomed", ["users.manage", "roles.manage"])

    await service.set_permissions(doomed.id, ["query.execute"])  # allowed


async def test_roles_cannot_be_assigned_across_tenants(account: Account) -> None:
    """RLS makes this a 404 rather than a 403: from here, that user does not exist."""
    with pytest.raises(NotFoundError):
        await RoleService(admin(account)).assign(uuid4(), [])


async def test_another_tenant_sees_no_roles(account: Account) -> None:
    intruder = AccessProfile(
        user_id=uuid4(),
        context=TenantContext.for_tenant(uuid4()),
        permissions=frozenset({"roles.manage"}),
    )

    assert await RoleService(intruder).visible() == []


# --- llm configuration -------------------------------------------------------------


async def test_an_unconfigured_tenant_reports_the_installation_default(
    account: Account,
) -> None:
    """ "Not configured" and "using the installation's model" look identical to an
    administrator otherwise, and only one of them is a problem."""
    settings = await LlmConfigService(admin(account)).get()

    assert settings.configured is False
    assert settings.endpoint_url


async def test_the_api_key_is_never_returned(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An administration screen that displays a credential turns every support screenshot
    into a disclosure, and nobody needs to read it — only to replace it."""
    from cryptography.fernet import Fernet

    from app.core.config import settings as configuration

    monkeypatch.setattr(configuration, "encryption_key", Fernet.generate_key().decode())
    service = LlmConfigService(admin(account))

    stored = await service.put("http://gateway/v1", "their-model", "sk-secret-value")

    assert stored.has_api_key is True
    assert "sk-secret-value" not in str(stored)

    async with owner_session() as session:
        raw = await session.scalar(
            text("SELECT api_key_encrypted FROM llm_config WHERE tenant_id = :t"),
            {"t": account.tenant_id},
        )
    assert raw and "sk-secret-value" not in raw, "and not in the database either"


async def test_saving_without_a_key_keeps_the_stored_one(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An administrator editing a model name cannot re-enter a credential they cannot
    read, and wiping it on a save that looked harmless would break generation."""
    from cryptography.fernet import Fernet

    from app.core.config import settings as configuration

    monkeypatch.setattr(configuration, "encryption_key", Fernet.generate_key().decode())
    service = LlmConfigService(admin(account))
    await service.put("http://gateway/v1", "first-model", "sk-secret-value")

    updated = await service.put("http://gateway/v1", "second-model")

    assert updated.model_name == "second-model"
    assert updated.has_api_key is True


async def test_clearing_falls_back_to_the_installation_default(account: Account) -> None:
    service = LlmConfigService(admin(account))
    await service.put("http://gateway/v1", "their-model")

    await service.clear()

    assert (await service.get()).configured is False


async def test_another_tenant_cannot_read_the_configuration(account: Account) -> None:
    """It holds an endpoint and a key: a customer's commercial relationship with a model
    provider, and one of the few rows here that is interesting on its own."""
    await LlmConfigService(admin(account)).put("http://gateway/v1", "their-model")
    intruder = AccessProfile(
        user_id=uuid4(),
        context=TenantContext.for_tenant(uuid4()),
        permissions=frozenset({"llm_config.manage"}),
    )

    assert (await LlmConfigService(intruder).get()).configured is False


# --- invitations -------------------------------------------------------------------


async def test_an_invited_user_sets_their_own_password_from_the_link(
    account: Account,
) -> None:
    """The link is the whole product of this endpoint, so it has to work end to end.

    There is still no email server on an on-premise install, so it is returned once and
    passed on by the administrator — exactly as a generated password used to be. What
    changed is that the credential is chosen by the person it belongs to, and never travels.
    """
    from app.features.auth.onboarding.credentials import CredentialTokens
    from app.features.auth.onboarding.invitations import InvitationService
    from app.features.auth.service import AuthService

    service = RoleService(admin(account))
    member = next(role for role in await service.visible() if role.name == "member")

    invitation = await InvitationService(admin(account)).invite("new@example.com", [member.id])
    await CredentialTokens().redeem(invitation.token, "a-password-they-chose")
    tokens = await AuthService().authenticate("new@example.com", "a-password-they-chose")

    assert tokens.access_token


async def test_the_link_works_exactly_once(account: Account) -> None:
    """The property that makes this better than a password in a chat window.

    A password pasted into a chat is a working credential for as long as the account
    exists. This is worthless the moment it has been used, so the same paste — in the same
    chat log, read by the same people later — opens nothing.
    """
    from app.features.auth.onboarding.credentials import CredentialTokens
    from app.features.auth.onboarding.invitations import InvitationService

    invitation = await InvitationService(admin(account)).invite("once@example.com", [])
    await CredentialTokens().redeem(invitation.token, "the-first-password")

    with pytest.raises(NotFoundError):
        await CredentialTokens().redeem(invitation.token, "a-second-attempt")


async def test_an_invitation_never_returns_a_password(account: Account) -> None:
    """Not an omission — the account is created with 32 bytes nobody keeps.

    If a password came back here as well, the link would be an extra step protecting
    nothing: the thing in the administrator's clipboard would still be a permanent
    credential.
    """
    from app.features.auth.onboarding.invitations import InvitationService

    invitation = await InvitationService(admin(account)).invite(f"link-{uuid4()}@example.com", [])

    assert not hasattr(invitation, "password")
    assert invitation.token


async def test_the_token_is_never_stored_in_the_clear(account: Account) -> None:
    """A database dump must not be a set of working links into every account.

    Backups end up on laptops and in support tickets. The row holds sha256 of the token and
    cannot produce a usable link.
    """
    from app.features.auth.onboarding.invitations import InvitationService

    invitation = await InvitationService(admin(account)).invite(f"hash-{uuid4()}@example.com", [])

    async with owner_session() as session:
        stored = await session.scalar(
            text("SELECT token_hash FROM credential_tokens WHERE user_id = :u"),
            {"u": invitation.user_id},
        )

    assert stored and invitation.token not in stored


async def test_a_link_is_generated_not_predictable(account: Account) -> None:
    from app.features.auth.onboarding.invitations import InvitationService

    first = await InvitationService(admin(account)).invite(f"one-{uuid4()}@example.com", [])
    second = await InvitationService(admin(account)).invite(f"two-{uuid4()}@example.com", [])

    assert first.token != second.token
    assert len(first.token) >= 20


async def test_inviting_an_existing_address_is_a_conflict(account: Account) -> None:
    """The administrator is entitled to know about their own users."""
    from app.features.auth.onboarding.invitations import InvitationService

    with pytest.raises(ConflictError):
        await InvitationService(admin(account)).invite(account.member_email, [])


async def test_an_unknown_role_is_refused_before_the_user_exists(account: Account) -> None:
    """Validated first, so a bad request cannot leave a user with no roles and an
    administrator wondering whether the invitation half-worked."""
    from app.features.auth.onboarding.invitations import InvitationService

    with pytest.raises(NotFoundError):
        await InvitationService(admin(account)).invite("d@example.com", [uuid4()])

    async with owner_session() as session:
        assert (
            await session.scalar(text("SELECT count(*) FROM users WHERE email = 'd@example.com'"))
            == 0
        )


async def test_an_invitation_lands_in_the_inviter_s_tenant(account: Account) -> None:
    """RLS decides where the row goes: the tenant comes from the caller's context, never
    from the request."""
    from app.features.auth.onboarding.invitations import InvitationService

    invitation = await InvitationService(admin(account)).invite("e@example.com", [])

    async with owner_session() as session:
        tenant_id = await session.scalar(
            text("SELECT tenant_id FROM users WHERE id = :u"), {"u": invitation.user_id}
        )

    assert tenant_id == account.tenant_id
