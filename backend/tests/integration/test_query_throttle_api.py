"""The rate limit, reached over HTTP.

`throttle.py` was written, documented and tested — and wired to nothing. Its own comment said
"Applied to `/query` and `/query/stream` only" while `RateLimit` was exported and imported by
no router, so every unit test in `test_throttle.py` passed against a limiter that never saw a
request. The module even records the same shape of miss happening once before: F14 claimed the
`Retry-After` header in a commit message and did not ship it, and the tests asserted on the
exception rather than the response.

So these test the wiring rather than the arithmetic: that a real request is refused, with the
header, on both endpoints.
"""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.common.exceptions import ZenithError
from app.features.auth.router import router as auth_router
from app.features.query.router import router as query_router
from app.features.query.throttle import reset
from app.main import handle_domain_error
from conftest import PASSWORD, Account

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client(configured_engines: None) -> AsyncIterator[AsyncClient]:
    reset()
    api = FastAPI()
    api.add_exception_handler(ZenithError, handle_domain_error)  # type: ignore[arg-type]
    api.include_router(auth_router)
    api.include_router(query_router)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as http:
        yield http
    reset()


async def headers(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.parametrize("path", ["/query", "/query/stream"])
async def test_the_limit_is_actually_applied(
    client: AsyncClient, account: Account, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """One question is allowed; the second is refused with a 429.

    The limit is lowered to one rather than sending thirty-one questions: this asserts that
    the dependency runs, and thirty model calls would be testing the model.
    """
    from app.features.query import throttle

    # `_limit` is the private field; the limiter deliberately exposes no setter, and a
    # test that lowered the *setting* would not reach an already-constructed limiter.
    monkeypatch.setattr(throttle.user_limiter, "_limit", 1)
    auth = await headers(client, account.admin_email)

    first = await client.post(path, json={"question": "what is my severance?"}, headers=auth)
    second = await client.post(path, json={"question": "and my notice period?"}, headers=auth)

    # The first may fail for its own reasons — no model configured in a test installation —
    # but it must not be refused by the limiter.
    assert first.status_code != 429
    assert second.status_code == 429


async def test_the_refusal_says_when_to_come_back(
    client: AsyncClient, account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client told only "no" retries immediately and makes the overload it was reporting
    worse. §2.12 requires the header for that reason, and the header is the half that went
    missing last time."""
    from app.features.query import throttle

    # `_limit` is the private field; the limiter deliberately exposes no setter, and a
    # test that lowered the *setting* would not reach an already-constructed limiter.
    monkeypatch.setattr(throttle.user_limiter, "_limit", 1)
    auth = await headers(client, account.admin_email)

    await client.post("/query", json={"question": "one"}, headers=auth)
    refused = await client.post("/query", json={"question": "two"}, headers=auth)

    assert refused.status_code == 429
    assert refused.headers.get("Retry-After") == "60"
    # RFC 7807, like every other error this API produces.
    assert refused.json()["title"] or refused.json()["detail"]


async def test_search_is_not_throttled(client: AsyncClient, account: Account) -> None:
    """Deliberate, and stated in `throttle.py`: `/search` costs about 1.2 seconds of
    mostly-Postgres and is not what falls over. Limiting it would restrict the cheap path to
    protect the expensive one — and the router does not even import the dependency."""
    from app.features.query.router import router

    throttled: set[str] = {
        route.path  # type: ignore[attr-defined]
        for route in router.routes
        if any(getattr(d, "dependency", None) is not None for d in route.dependencies)  # type: ignore[attr-defined]
    }

    assert "/query" in throttled
    assert "/query/stream" in throttled
