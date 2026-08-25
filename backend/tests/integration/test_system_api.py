"""`/system/*` over real HTTP, and the one assertion the whole design rests on.

`test_isolation` already proves that `requires_system_admin` refuses a tenant administrator
holding the entire `CATALOGUE`. What it cannot prove is that the routes *use* it: the
dependency is declared once, on the `APIRouter`, and a router-level dependency is exactly the
kind of thing an edit drops without any service or unit test noticing.

The consequence of dropping it is not subtle. `POST /system/tenants/{id}/purge` destroys an
organisation and everything in it.
"""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.common.exceptions import ZenithError
from app.core.database import owner_session
from app.features.auth.router import router as auth_router
from app.features.system.router import router as system_router
from app.main import handle_domain_error
from conftest import PASSWORD, Account

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client(configured_engines: None) -> AsyncIterator[AsyncClient]:
    api = FastAPI()
    api.add_exception_handler(ZenithError, handle_domain_error)  # type: ignore[arg-type]
    api.include_router(auth_router)
    api.include_router(system_router)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as http:
        yield http


async def headers(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def make_system_admin(user_id: object) -> None:
    """Through the owner connection, which is the only way — and the point of the test below
    that asserts the application role cannot do this."""
    async with owner_session() as session:
        await session.execute(
            text("UPDATE users SET is_system_admin = true WHERE id = :u"), {"u": user_id}
        )


ROUTES = [
    ("get", "/system/tenants", None),
    ("post", "/system/tenants", {"name": "Nope", "admin_email": "nobody@example.com"}),
    ("post", f"/system/tenants/{uuid4()}/suspend", None),
    ("post", f"/system/tenants/{uuid4()}/activate", None),
    ("post", f"/system/tenants/{uuid4()}/purge", {"confirm_name": "Nope"}),
]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
async def test_a_tenant_administrator_is_refused_over_http(
    client: AsyncClient, account: Account, method: str, path: str, body: dict[str, str] | None
) -> None:
    """The `admin` role holds the whole catalogue and is not a system administrator.

    Asserted per route rather than once, because the dependency is declared on the router: if
    it were moved onto the individual routes and one were missed, a single check would still
    pass.
    """
    auth = await headers(client, account.admin_email)
    call = getattr(client, method)
    response = await (call(path, json=body, headers=auth) if body else call(path, headers=auth))

    assert response.status_code == 403, f"{method.upper()} {path} admitted a tenant administrator"


async def test_a_system_administrator_reaches_it(client: AsyncClient, account: Account) -> None:
    """The other half: the refusal above is the flag doing its work, not the routes being
    broken."""
    await make_system_admin(account.admin_id)

    response = await client.get(
        "/system/tenants", headers=await headers(client, account.admin_email)
    )

    assert response.status_code == 200
    assert any(str(account.tenant_id) == row["id"] for row in response.json())


async def test_purging_an_active_organisation_is_refused_over_http(
    client: AsyncClient, account: Account
) -> None:
    """The first of the two brakes, reached through the API rather than the service.

    Suspending is reversible and purging is not, so the order is the safeguard. Both brakes
    are tested at the service level; this asserts the route does not route around them.
    """
    await make_system_admin(account.admin_id)
    auth = await headers(client, account.admin_email)

    async with owner_session() as session:
        name = await session.scalar(
            text("SELECT name FROM tenants WHERE id = :t"), {"t": account.tenant_id}
        )

    response = await client.post(
        f"/system/tenants/{account.tenant_id}/purge", json={"confirm_name": name}, headers=auth
    )

    assert response.status_code == 409
