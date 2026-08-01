import time
from uuid import uuid4

import pytest

from app.common.exceptions import AuthenticationError
from app.core.database import owner_session
from app.core.security import decode_token, issue_token
from app.features.auth.model import User
from app.features.auth.service import AuthService, normalise_email
from conftest import PASSWORD, Account

pytestmark = pytest.mark.asyncio


async def test_correct_password_authenticates(account: Account) -> None:
    pair = await AuthService().authenticate(account.admin_email, PASSWORD)

    payload = decode_token(pair.access_token, "access")
    assert payload["sub"] == str(account.admin_id)
    assert payload["tid"] == str(account.tenant_id)


async def test_wrong_password_is_refused(account: Account) -> None:
    with pytest.raises(AuthenticationError):
        await AuthService().authenticate(account.admin_email, "not-the-password")


async def test_unknown_email_is_refused(account: Account) -> None:
    with pytest.raises(AuthenticationError):
        await AuthService().authenticate(f"nobody-{uuid4()}@example.com", PASSWORD)


async def test_email_case_does_not_matter(account: Account) -> None:
    """Someone invited as `Ana@x.com` who types `ana@x.com` must not be told their
    password is wrong."""
    await AuthService().authenticate(account.admin_email.upper(), PASSWORD)


async def test_unknown_email_costs_the_same_as_a_wrong_password(account: Account) -> None:
    """No account-enumeration oracle.

    If verification were skipped for unknown addresses, a fast rejection would mean
    "no such user" and a slow one "wrong password", and the login form would become a
    way to harvest valid addresses. The bound is loose on purpose: this is checking
    that the hash runs at all, not benchmarking argon2 on CI hardware.
    """
    service = AuthService()

    started = time.perf_counter()
    with pytest.raises(AuthenticationError):
        await service.authenticate(account.admin_email, "not-the-password")
    known = time.perf_counter() - started

    started = time.perf_counter()
    with pytest.raises(AuthenticationError):
        await service.authenticate(f"nobody-{uuid4()}@example.com", PASSWORD)
    unknown = time.perf_counter() - started

    assert unknown > known / 3


async def test_an_access_token_is_not_accepted_where_a_refresh_is_expected(
    account: Account,
) -> None:
    """`typ` is checked, not assumed. Without it, the 15-minute access token would be
    usable for 14 days by handing it to the refresh endpoint."""
    pair = await AuthService().authenticate(account.admin_email, PASSWORD)

    with pytest.raises(AuthenticationError):
        await AuthService().refresh(pair.access_token)


async def test_a_refresh_token_is_not_accepted_as_an_access_token(account: Account) -> None:
    pair = await AuthService().authenticate(account.admin_email, PASSWORD)

    with pytest.raises(AuthenticationError):
        AuthService().principal(pair.refresh_token)


async def test_a_tampered_token_is_refused(account: Account) -> None:
    pair = await AuthService().authenticate(account.admin_email, PASSWORD)

    with pytest.raises(AuthenticationError):
        AuthService().principal(pair.access_token[:-4] + "aaaa")


async def test_refresh_returns_a_working_pair(account: Account) -> None:
    pair = await AuthService().authenticate(account.admin_email, PASSWORD)

    renewed = await AuthService().refresh(pair.refresh_token)

    user_id, tenant_id = AuthService().principal(renewed.access_token)
    assert (user_id, tenant_id) == (account.admin_id, account.tenant_id)


async def test_a_stale_token_version_is_refused_on_refresh(account: Account) -> None:
    """Revocation on the one path that reads the database.

    Access tokens are never checked against a table, so a revoked session survives
    until its access token expires — at most 15 minutes. What must not survive is the
    refresh, or the session would come back for another fourteen days.
    """
    stale = issue_token("refresh", account.admin_id, account.tenant_id, token_version=0)

    async with owner_session() as session:
        user = await session.get(User, account.admin_id)
        assert user is not None
        user.token_version = 1

    with pytest.raises(AuthenticationError):
        await AuthService().refresh(stale)


async def test_normalise_email_lowercases_and_trims() -> None:
    assert normalise_email("  Ana@Example.COM ") == "ana@example.com"
