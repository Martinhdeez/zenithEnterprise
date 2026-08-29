"""The chain end to end: a login, a bearer token, and a permission decision.

The unit tests check that resolution is right. These check that the wiring actually
uses it — a correct `requires` that no endpoint depends on protects nothing.
"""

from collections.abc import AsyncIterator

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.common.exceptions import ZenithError
from app.features.auth.access.dependencies import requires
from app.features.auth.router import router as auth_router
from app.main import handle_domain_error
from conftest import PASSWORD, Account

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """The real router on a bare app.

    `app.main.app` is not reused because its lifespan runs `verify_rls_active` against
    whatever the environment points at; the engines these tests need are already
    redirected by `configured_engines`.
    """
    api = FastAPI()
    api.add_exception_handler(ZenithError, handle_domain_error)  # type: ignore[arg-type]
    api.include_router(auth_router)

    async def protected() -> dict[str, bool]:
        return {"ok": True}

    # Registered rather than decorated so the handler is visibly referenced; the
    # decorator form reads as dead code to a type checker.
    api.add_api_route("/protected", protected, dependencies=[Depends(requires("documents.upload"))])

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def _login(client: AsyncClient, email: str) -> str:
    response = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200
    return response.json()["access_token"]


async def test_login_returns_a_usable_pair(client: AsyncClient, account: Account) -> None:
    response = await client.post(
        "/auth/login", json={"email": account.admin_email, "password": PASSWORD}
    )

    body = response.json()
    assert response.status_code == 200
    assert body["token_type"] == "bearer"
    assert body["access_token"] and body["refresh_token"]


async def test_wrong_password_is_401_and_says_nothing_useful(
    client: AsyncClient, account: Account
) -> None:
    response = await client.post(
        "/auth/login", json={"email": account.admin_email, "password": "wrong-password"}
    )

    assert response.status_code == 401
    # The same message an unknown address gets. Distinguishing them would confirm which
    # addresses are registered.
    assert response.json()["message"] == "invalid email or password"


async def test_me_reports_what_the_caller_reaches(client: AsyncClient, account: Account) -> None:
    token = await _login(client, account.admin_email)

    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    body = response.json()
    assert body["user_id"] == str(account.admin_id)
    assert body["tenant_id"] == str(account.tenant_id)
    assert set(body["label_ids"]) == {
        str(account.finance_label),
        str(account.hr_label),
        str(account.default_label),
        # `admin` alone reaches the quarantine label, which is what lets an unfiled upload be
        # readable by somebody able to classify it without being readable by the tenant.
        str(account.quarantine_label),
    }
    assert "documents.upload" in body["permissions"]


async def test_a_protected_endpoint_refuses_without_the_permission(
    client: AsyncClient, account: Account
) -> None:
    """`member` can query but cannot upload, and the refusal comes from the dependency,
    not from anything the handler remembered to write."""
    token = await _login(client, account.member_email)

    response = await client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403


async def test_a_protected_endpoint_allows_with_the_permission(
    client: AsyncClient, account: Account
) -> None:
    token = await _login(client, account.admin_email)

    response = await client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200


async def test_login_is_rate_limited(client: AsyncClient, account: Account) -> None:
    """The limit is wired to the endpoint, not merely implemented.

    A limiter nothing depends on protects nothing, and this is the endpoint where that
    matters most: every failed attempt costs an argon2 verification whether or not the
    address exists.
    """
    from app.core.config import settings
    from app.features.auth import router as router_module
    from app.features.auth.throttle import SlidingWindowLimiter

    original = router_module.login_limiter
    router_module.login_limiter = SlidingWindowLimiter(limit=2, window_seconds=60.0)
    try:
        codes = [
            (
                await client.post(
                    "/auth/login", json={"email": account.admin_email, "password": "wrong-one"}
                )
            ).status_code
            for _ in range(3)
        ]
    finally:
        router_module.login_limiter = original

    assert codes == [401, 401, 429]
    assert settings.login_attempts_per_minute > 0


async def test_a_protected_endpoint_refuses_without_a_token(client: AsyncClient) -> None:
    """The failure is closed: no header means 401, never an anonymous pass."""
    response = await client.get("/protected")

    assert response.status_code == 401


async def test_a_garbage_token_is_refused(client: AsyncClient) -> None:
    response = await client.get("/protected", headers={"Authorization": "Bearer not-a-jwt"})

    assert response.status_code == 401


async def test_refresh_over_http_returns_a_new_pair(client: AsyncClient, account: Account) -> None:
    login = await client.post(
        "/auth/login", json={"email": account.admin_email, "password": PASSWORD}
    )

    response = await client.post(
        "/auth/refresh", json={"refresh_token": login.json()["refresh_token"]}
    )

    assert response.status_code == 200
    assert response.json()["access_token"]
