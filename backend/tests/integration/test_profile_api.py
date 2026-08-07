"""The profile screen's endpoints, and the guard on the one that changes a credential.

Password change is the part worth testing hardest. Until it existed a password was
generated at invitation and could only be replaced by an administrator with shell access,
so adding it is a net improvement — but adding it *without* requiring the current password
would turn a stolen access token from a session into a permanent account takeover, which
is strictly worse than the gap it closes.
"""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.common.exceptions import ZenithError
from app.core.database import owner_session
from app.features.auth.router import router as auth_router
from app.main import handle_domain_error
from conftest import PASSWORD, Account

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client(configured_engines: None) -> AsyncIterator[AsyncClient]:
    api = FastAPI()
    api.add_exception_handler(ZenithError, handle_domain_error)  # type: ignore[arg-type]
    api.include_router(auth_router)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as http:
        yield http


async def token(client: AsyncClient, email: str, password: str = PASSWORD) -> str:
    response = await client.post("/auth/login", json={"email": email, "password": password})
    return response.json()["access_token"]


async def headers(client: AsyncClient, email: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {await token(client, email)}"}


async def test_the_profile_names_things_rather_than_listing_ids(
    client: AsyncClient, account: Account
) -> None:
    """`/auth/me` already returned ids and permissions. This exists because none of that
    tells somebody which organisation they are in or which compartments they reach."""
    response = await client.get("/auth/profile", headers=await headers(client, account.admin_email))

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == account.admin_email
    assert body["tenant_name"], "the organisation should be named, not just identified"
    assert "admin" in body["roles"]
    # Finance and General, by name — the answer to "why can I not see that document".
    assert "Finance" in body["labels"]


async def test_the_profile_only_names_labels_the_caller_reaches(
    client: AsyncClient, account: Account
) -> None:
    """A label list is a disclosure like any other. The member's role reaches the default
    label and not Finance, and naming Finance here would tell them a compartment exists
    that they were not admitted to."""
    response = await client.get(
        "/auth/profile", headers=await headers(client, account.member_email)
    )

    assert "Finance" not in response.json()["labels"]


async def test_changing_a_password_requires_the_current_one(
    client: AsyncClient, account: Account
) -> None:
    """The assertion this endpoint lives or dies on.

    Without it, an access token in the wrong hands stops being a session that expires and
    becomes a permanent takeover: change the password and the owner is locked out of their
    own tenant.
    """
    response = await client.post(
        "/auth/password",
        json={"current_password": "not-the-password", "new_password": "a-new-one-entirely"},
        headers=await headers(client, account.admin_email),
    )

    assert response.status_code == 401
    # And nothing changed — the original still works.
    assert (
        await client.post("/auth/login", json={"email": account.admin_email, "password": PASSWORD})
    ).status_code == 200


async def test_a_changed_password_is_the_one_that_works(
    client: AsyncClient, account: Account
) -> None:
    changed = await client.post(
        "/auth/password",
        json={"current_password": PASSWORD, "new_password": "a-new-one-entirely"},
        headers=await headers(client, account.admin_email),
    )
    assert changed.status_code == 204

    assert (
        await client.post("/auth/login", json={"email": account.admin_email, "password": PASSWORD})
    ).status_code == 401
    assert (
        await client.post(
            "/auth/login",
            json={"email": account.admin_email, "password": "a-new-one-entirely"},
        )
    ).status_code == 200


async def test_changing_a_password_stops_the_other_sessions_renewing(
    client: AsyncClient, account: Account
) -> None:
    """Asserted on refresh, not on the next request, because that is where the mechanism
    actually is.

    Access tokens are stateless by design (mvp.md 2.4): verifying one reads no database,
    so a live one keeps working until it expires. What a bumped `token_version` prevents
    is renewal — which bounds any session to one access-token lifetime and is the whole
    guarantee this offers. A test asserting instant death would be asserting a revocation
    model this product deliberately did not buy.
    """
    elsewhere = (
        await client.post("/auth/login", json={"email": account.admin_email, "password": PASSWORD})
    ).json()["refresh_token"]

    await client.post(
        "/auth/password",
        json={"current_password": PASSWORD, "new_password": "a-new-one-entirely"},
        headers=await headers(client, account.admin_email),
    )

    renewed = await client.post("/auth/refresh", json={"refresh_token": elsewhere})
    assert renewed.status_code == 401, "the other session must not be able to renew itself"


async def test_signing_out_everywhere_includes_the_session_that_asked(
    client: AsyncClient, account: Account
) -> None:
    """Sparing the calling device would leave alive the one session an attacker is most
    likely to be holding — so its refresh token has to be dead too, not just everyone
    else's."""
    issued = (
        await client.post("/auth/login", json={"email": account.admin_email, "password": PASSWORD})
    ).json()
    auth = {"Authorization": f"Bearer {issued['access_token']}"}

    assert (await client.post("/auth/sign-out-everywhere", headers=auth)).status_code == 204

    renewed = await client.post("/auth/refresh", json={"refresh_token": issued["refresh_token"]})
    assert renewed.status_code == 401


async def test_the_profile_shows_a_name_when_there_is_one(
    client: AsyncClient, account: Account
) -> None:
    """Nullable by design — existing users have none and an invitation cannot demand one —
    so what matters is that it is carried when set and that nothing breaks when it is not."""
    before = await client.get("/auth/profile", headers=await headers(client, account.admin_email))
    assert before.json()["name"] is None

    async with owner_session() as session:
        await session.execute(
            text("UPDATE users SET name = :n WHERE id = :i"),
            {"n": "Ada Lovelace", "i": account.admin_id},
        )

    after = await client.get("/auth/profile", headers=await headers(client, account.admin_email))
    assert after.json()["name"] == "Ada Lovelace"
