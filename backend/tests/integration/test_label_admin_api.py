"""`GET /labels/search` and `POST /labels/merge` over the real HTTP surface.

The service tests in `app/features/labels/tests/` cover the logic; these cover the parts
only the wire can get wrong — that the permission gate is actually attached, that a
cursor survives being serialised into a query string and handed back, and that a refusal
arrives as the status code a client can branch on rather than a 500.

Everything here runs as the application database role, so what is exercised is what a
customer runs.
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.common.exceptions import ZenithError
from app.core.database import owner_session
from app.features.auth.router import router as auth_router
from app.features.labels.model import AccessLabel, RoleLabel
from app.features.labels.router import router as labels_router
from app.main import handle_domain_error
from conftest import PASSWORD, Account

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    api = FastAPI()
    api.add_exception_handler(ZenithError, handle_domain_error)  # type: ignore[arg-type]
    api.include_router(auth_router)
    api.include_router(labels_router)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as http:
        yield http


async def headers(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def label(tenant_id: UUID, name: str, *, reachable_by_admin: bool = True) -> UUID:
    """A label, seeded through the owner connection.

    `reachable_by_admin` because merge requires the caller to reach every label involved,
    and the admin's own reach is what makes the difference between a 200 and a 403.
    """
    async with owner_session() as session:
        created = AccessLabel(tenant_id=tenant_id, name=name)
        session.add(created)
        await session.flush()
        if reachable_by_admin:
            admin_role = await session.scalar(
                text("SELECT id FROM roles WHERE tenant_id = :t AND name = 'admin'"),
                {"t": tenant_id},
            )
            session.add(RoleLabel(role_id=UUID(str(admin_role)), label_id=created.id))
        return created.id


async def test_search_returns_a_page_with_usage_counts(
    client: AsyncClient, account: Account
) -> None:
    await label(account.tenant_id, "Quarterly Reports")

    response = await client.get(
        "/labels/search?q=quarterly", headers=await headers(client, account.admin_email)
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["name"] for item in body["items"]] == ["Quarterly Reports"]
    assert body["items"][0]["documents"] == 0
    assert body["next_cursor"] is None


async def test_a_cursor_survives_the_round_trip_through_the_query_string(
    client: AsyncClient, account: Account
) -> None:
    """The cursor is base64 with the padding stripped, which is exactly the shape that
    breaks when something along the way decides to unescape it. Asserted end to end
    because no unit test of the encoder can see a URL."""
    for index in range(3):
        await label(account.tenant_id, f"Page {index}")
    auth = await headers(client, account.admin_email)

    first = (await client.get("/labels/search?q=Page&limit=2", headers=auth)).json()
    assert first["next_cursor"] is not None

    second = (
        await client.get(
            f"/labels/search?q=Page&limit=2&cursor={first['next_cursor']}", headers=auth
        )
    ).json()

    seen = [item["name"] for item in first["items"] + second["items"]]
    assert seen == ["Page 0", "Page 1", "Page 2"]
    assert second["next_cursor"] is None


async def test_a_forged_cursor_is_a_400_not_a_500(client: AsyncClient, account: Account) -> None:
    response = await client.get(
        "/labels/search?cursor=nonsense", headers=await headers(client, account.admin_email)
    )

    assert response.status_code == 400


async def test_a_member_cannot_merge(client: AsyncClient, account: Account) -> None:
    """The `labels.manage` gate, asserted at the surface it is attached to. A member
    holding no such permission must not be able to move documents between roles."""
    duplicate = await label(account.tenant_id, "Finanace")

    response = await client.post(
        "/labels/merge",
        json={"sources": [str(duplicate)], "target": str(account.finance_label)},
        headers=await headers(client, account.member_email),
    )

    assert response.status_code == 403


async def test_a_dry_run_reports_without_changing_anything(
    client: AsyncClient, account: Account
) -> None:
    duplicate = await label(account.tenant_id, "Finanace")

    response = await client.post(
        "/labels/merge",
        json={
            "sources": [str(duplicate)],
            "target": str(account.finance_label),
            "dry_run": True,
        },
        headers=await headers(client, account.admin_email),
    )

    assert response.status_code == 200
    assert response.json()["dry_run"] is True

    async with owner_session() as session:
        still_there = await session.scalar(
            text("SELECT 1 FROM access_labels WHERE id = :id"), {"id": duplicate}
        )
    assert still_there is not None


async def test_a_widening_merge_is_a_409_the_client_can_branch_on(
    client: AsyncClient, account: Account
) -> None:
    """The refusal has to arrive as a status code, not as prose in a 500. A client that
    cannot tell "this needs confirming" from "this broke" will retry the wrong one."""
    restricted = await label(account.tenant_id, "Restricted")
    wide = await label(account.tenant_id, "Wide")
    async with owner_session() as session:
        extra = await session.scalar(
            text("INSERT INTO roles (tenant_id, name) VALUES (:t, :n) RETURNING id"),
            {"t": account.tenant_id, "n": f"role-{uuid4()}"},
        )
        session.add(RoleLabel(role_id=UUID(str(extra)), label_id=wide))
        document_id = await session.scalar(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes) "
                "VALUES (:t, 'secret.pdf', :sha, 10) RETURNING id"
            ),
            {"t": account.tenant_id, "sha": uuid4().hex + uuid4().hex[:32]},
        )
        await session.execute(
            text("INSERT INTO document_labels (document_id, label_id) VALUES (:d, :l)"),
            {"d": document_id, "l": restricted},
        )

    auth = await headers(client, account.admin_email)
    body = {"sources": [str(restricted)], "target": str(wide)}

    refused = await client.post("/labels/merge", json=body, headers=auth)
    assert refused.status_code == 409

    accepted = await client.post(
        "/labels/merge", json={**body, "acknowledge_widening": True}, headers=auth
    )
    assert accepted.status_code == 200
    assert accepted.json()["visibility_widening"] == 1
