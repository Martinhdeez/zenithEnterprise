"""Search over real HTTP.

The service tests cover the decisions. These cover the surface: the permission in the
signature, the contract shape a client depends on, and the two places an error message could
disclose a compartment the caller is locked out of.
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
from app.features.retrieval.router import router as search_router
from app.main import handle_domain_error
from conftest import PASSWORD, Account

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client(configured_engines: None) -> AsyncIterator[AsyncClient]:
    api = FastAPI()
    api.add_exception_handler(ZenithError, handle_domain_error)  # type: ignore[arg-type]
    api.include_router(auth_router)
    api.include_router(search_router)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as http:
        yield http


async def headers(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def seed(account: Account, label_id: str | None = None) -> None:
    async with owner_session() as session:
        document_id = await session.scalar(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes, status) "
                "VALUES (:t, 'handbook.pdf', :sha, 10, 'ready') RETURNING id"
            ),
            {"t": account.tenant_id, "sha": str(uuid4())},
        )
        await session.execute(
            text("INSERT INTO document_labels (document_id, label_id) VALUES (:d, :l)"),
            {"d": document_id, "l": label_id or account.default_label},
        )
        await session.execute(
            text(
                "INSERT INTO chunks (document_id, tenant_id, page_num, char_start, char_end, "
                "text, bboxes) VALUES (:d, :t, 7, 0, 60, "
                "'Holiday entitlement accrues monthly for every employee.', "
                '\'[{"page": 7, "x0": 0.1, "y0": 0.2, "x1": 0.9, "y1": 0.3}]\')'
            ),
            {"d": document_id, "t": account.tenant_id},
        )


async def test_search_returns_the_citation_payload(client: AsyncClient, account: Account) -> None:
    """The shape F7's viewer depends on: which document, which page, and where on it.

    Bounding boxes exist only because ingestion recorded them. Discovering later that the
    contract omits them means re-parsing the whole corpus, which F5 measured in hours.
    """
    await seed(account)

    response = await client.get(
        "/search?q=holiday entitlement", headers=await headers(client, account.admin_email)
    )

    body = response.json()
    assert response.status_code == 200
    assert body["hits"][0]["page_num"] == 7
    assert body["hits"][0]["filename"] == "handbook.pdf"
    assert body["hits"][0]["bboxes"][0]["x0"] == 0.1
    assert body["hits"][0]["lexical_rank"] == 1
    assert "took_ms" in body


async def test_search_reports_when_it_ran_on_one_half(
    client: AsyncClient, account: Account
) -> None:
    """No embedding service is reachable in the test environment, which is exactly the
    condition this flag exists for. A silently halved search is the failure this project
    keeps refusing."""
    await seed(account)

    body = (
        await client.get("/search?q=holiday", headers=await headers(client, account.admin_email))
    ).json()

    assert body["degraded"] is True
    assert "lexical" in body["reason"]
    assert body["hits"], "the lexical half must still answer"


async def test_searching_requires_the_permission(client: AsyncClient, account: Account) -> None:
    """`member` holds `query.execute`, so the check needs a role that does not."""
    async with owner_session() as session:
        await session.execute(
            text(
                "DELETE FROM role_permissions rp USING roles r "
                "WHERE rp.role_id = r.id AND r.tenant_id = :t "
                "AND rp.permission_code = 'query.execute'"
            ),
            {"t": account.tenant_id},
        )

    response = await client.get(
        "/search?q=anything", headers=await headers(client, account.member_email)
    )

    assert response.status_code == 403


async def test_a_label_the_caller_does_not_hold_is_a_403(
    client: AsyncClient, account: Account
) -> None:
    """Not an empty result: that would teach a client to probe other people's labels and
    watch which ones come back silent."""
    response = await client.get(
        f"/search?q=holiday&labels={account.finance_label}",
        headers=await headers(client, account.member_email),
    )

    assert response.status_code == 403


async def test_a_member_cannot_see_a_finance_passage(client: AsyncClient, account: Account) -> None:
    """End to end, through HTTP, with the member's own token — the composition F3 and F5
    each proved separately."""
    await seed(account, label_id=str(account.finance_label))

    body = (
        await client.get(
            "/search?q=holiday entitlement", headers=await headers(client, account.member_email)
        )
    ).json()

    assert body["hits"] == []


async def test_an_empty_query_is_rejected_by_the_schema(
    client: AsyncClient, account: Account
) -> None:
    response = await client.get("/search?q=", headers=await headers(client, account.admin_email))

    assert response.status_code == 422
