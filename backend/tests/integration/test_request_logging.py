"""The two fields that make a log usable during an incident.

`request_context`'s first paragraph names them: **which request** a line belongs to, and
**which tenant** it concerned. Only the first was ever bound. `bind_tenant` existed with a
body of `del request` and a comment claiming the caller had already done it — no caller had —
and `log_response` was written and called by nothing, so an installation nobody can SSH into
had no access log at all.

We sell on-premise. "It was slow this morning" is answered from whatever the customer can
paste into an email, and these are the lines that answer it.
"""

from collections.abc import AsyncIterator, Iterator

import pytest
import structlog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.common.exceptions import ZenithError
from app.core.request_context import RequestContextMiddleware
from app.features.auth.access.dependencies import CurrentProfile
from app.features.auth.router import router as auth_router
from app.features.documents.router import router as documents_router
from app.main import handle_domain_error
from conftest import PASSWORD, Account

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client(configured_engines: None) -> AsyncIterator[AsyncClient]:
    api = FastAPI()
    api.add_exception_handler(ZenithError, handle_domain_error)  # type: ignore[arg-type]
    api.add_middleware(RequestContextMiddleware)
    api.include_router(auth_router)
    api.include_router(documents_router)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as http:
        yield http


@pytest.fixture
async def probing(configured_engines: None) -> AsyncIterator[AsyncClient]:
    """The same app with one extra route that reports what is bound to the log context."""
    api = FastAPI()
    api.add_exception_handler(ZenithError, handle_domain_error)  # type: ignore[arg-type]
    api.add_middleware(RequestContextMiddleware)
    api.include_router(auth_router)

    async def probe() -> dict[str, object]:
        return dict(structlog.contextvars.get_contextvars())

    api.get("/_probe")(probe)

    # The binding happens in the authentication dependency, so reaching it needs a route that
    # actually resolves a profile — which is the arrangement being tested, not a detail of
    # the harness.
    async def probe_authenticated(profile: CurrentProfile) -> dict[str, object]:
        del profile
        return dict(structlog.contextvars.get_contextvars())

    api.get("/_probe/auth")(probe_authenticated)

    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as http:
        yield http


@pytest.fixture
def captured() -> Iterator[list[dict[str, object]]]:
    """Every log event this process emits, as dictionaries.

    `capture_logs()` rather than reconfiguring structlog by hand. `configure_logging` sets
    `cache_logger_on_first_use=True`, so a logger bound once keeps the processors it was bound
    with — a fixture that reconfigures afterwards intercepts the first test in a file and
    nothing after it, which is exactly how this failed.
    """
    with structlog.testing.capture_logs() as entries:
        yield entries  # type: ignore[misc]


async def test_every_request_produces_one_line(
    client: AsyncClient, account: Account, captured: list[dict[str, object]]
) -> None:
    await client.get("/documents")

    requests = [entry for entry in captured if entry.get("event") == "request"]
    assert len(requests) == 1
    assert requests[0]["path"] == "/documents"
    assert requests[0]["method"] == "GET"
    # The number that answers "it was slow this morning", which is why this is a line of its
    # own rather than something inferred from two timestamps.
    assert isinstance(requests[0]["elapsed_ms"], float)


async def test_an_authenticated_request_binds_the_tenant(
    probing: AsyncClient, account: Account
) -> None:
    """The half that was missing, and the one that routes an incident to a customer.

    Read from the context rather than from a captured log line, deliberately.
    `structlog.testing.capture_logs` replaces the whole processor chain — `merge_contextvars`
    included — so asserting on the event would be asserting on the test harness. Every log
    emitted during the request carries whatever is bound here; that binding is the property.
    """
    response = await probing.post(
        "/auth/login", json={"email": account.admin_email, "password": PASSWORD}
    )
    token = response.json()["access_token"]

    bound = (await probing.get("/_probe/auth", headers={"Authorization": f"Bearer {token}"})).json()

    assert bound["tenant_id"] == str(account.tenant_id)
    # Every line in the request is attributable to one request, which is the other half.
    assert bound["trace_id"]
    # Deliberately absent: `request_context` explains that which employee asked what belongs
    # in the audit trail, not in an operational log a support engineer tails.
    assert "user_id" not in bound


async def test_an_unauthenticated_request_binds_no_tenant(probing: AsyncClient) -> None:
    """Bound from the authentication dependency, not the middleware.

    The middleware runs before anything has read the token, and a tenant guessed from an
    unverified header would be worse than no tenant at all.
    """
    bound = (await probing.get("/_probe")).json()

    assert "tenant_id" not in bound
    assert bound["trace_id"]


async def test_a_request_that_raises_is_still_logged(
    client: AsyncClient, captured: list[dict[str, object]]
) -> None:
    """The request most worth having a line for.

    Emitted from a `finally`, and the status stays 500 because nothing ever sent a response
    start — which is exactly what happened.
    """

    async def boom() -> None:
        raise RuntimeError("no")

    client._transport.app.get("/boom")(boom)  # type: ignore[attr-defined,union-attr]

    with pytest.raises(RuntimeError):
        await client.get("/boom")

    requests = [entry for entry in captured if entry.get("event") == "request"]
    assert requests
    assert requests[0]["status"] == 500
