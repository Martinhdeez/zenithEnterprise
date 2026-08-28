"""The administration surface over real HTTP.

Nine of this router's fifteen routes were never exercised through the API. Their services are
tested thoroughly — which is the point: a service test cannot see a permission declared in the
wrong signature, and everything on this router decides who may do what.

The one that matters most is `PUT /users/{user_id}/roles`. Access here has two halves — which
labels a role reaches, and which roles a user holds — and this is the second. A gate missing
from it is a member making themselves an administrator.
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.common.exceptions import ZenithError
from app.core.database import owner_session
from app.features.admin.router import router as admin_router
from app.features.auth.router import router as auth_router
from app.main import handle_domain_error
from conftest import PASSWORD, Account

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client(configured_engines: None) -> AsyncIterator[AsyncClient]:
    api = FastAPI()
    api.add_exception_handler(ZenithError, handle_domain_error)  # type: ignore[arg-type]
    api.include_router(auth_router)
    api.include_router(admin_router)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as http:
        yield http


async def headers(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def role_id(account: Account, name: str) -> UUID:
    async with owner_session() as session:
        found = await session.scalar(
            text("SELECT id FROM roles WHERE tenant_id = :t AND name = :n"),
            {"t": account.tenant_id, "n": name},
        )
    assert found is not None
    return found


class TestAssigningRoles:
    """The half of the access model this router owns."""

    async def test_a_member_cannot_change_anybody_s_roles(
        self, client: AsyncClient, account: Account
    ) -> None:
        """Including their own. A gate missing here is a member making themselves an
        administrator, which is the shortest path to every compartment in the tenant."""
        admin_role = await role_id(account, "admin")

        response = await client.put(
            f"/users/{account.member_id}/roles",
            json={"role_ids": [str(admin_role)]},
            headers=await headers(client, account.member_email),
        )

        assert response.status_code == 403

    async def test_an_administrator_can(self, client: AsyncClient, account: Account) -> None:
        admin_role = await role_id(account, "admin")

        response = await client.put(
            f"/users/{account.member_id}/roles",
            json={"role_ids": [str(admin_role)]},
            headers=await headers(client, account.admin_email),
        )

        assert response.status_code in {200, 204}
        async with owner_session() as session:
            held = list(
                await session.scalars(
                    text("SELECT role_id FROM user_roles WHERE user_id = :u"),
                    {"u": account.member_id},
                )
            )
        assert held == [admin_role]

    async def test_a_role_from_another_tenant_is_refused(
        self, client: AsyncClient, account: Account
    ) -> None:
        """RLS hides it, so the id resolves to nothing — and the answer must be a refusal
        rather than a silent assignment of an empty set."""
        response = await client.put(
            f"/users/{account.member_id}/roles",
            json={"role_ids": [str(uuid4())]},
            headers=await headers(client, account.admin_email),
        )

        assert response.status_code == 404


class TestTheModelConfiguration:
    async def test_a_member_cannot_read_it(self, client: AsyncClient, account: Account) -> None:
        response = await client.get(
            "/llm-config", headers=await headers(client, account.member_email)
        )

        assert response.status_code == 403

    async def test_the_key_is_never_returned(self, client: AsyncClient, account: Account) -> None:
        """`has_api_key`, not the key. The schema says so; this asserts the wire.

        An administrator who can read the credential back can copy it out of the product,
        and the reason it is encrypted at rest stops meaning anything.
        """
        auth = await headers(client, account.admin_email)
        stored = await client.put(
            "/llm-config",
            json={
                "endpoint_url": "https://example.invalid/v1",
                "model_name": "some-model",
                "api_key": "sk-secret-value",
            },
            headers=auth,
        )
        # Asserted rather than assumed. Unchecked, a 500 from the write left the read
        # reporting no key, and the failure read as `assert False is True` — which points
        # at the wrong half of the test. The write is a precondition here, not the subject.
        assert stored.status_code == 200, stored.text

        body = (await client.get("/llm-config", headers=auth)).json()

        assert body["has_api_key"] is True
        assert "sk-secret-value" not in str(body)
        assert "api_key" not in body

    async def test_clearing_it_falls_back_rather_than_breaking(
        self, client: AsyncClient, account: Account
    ) -> None:
        auth = await headers(client, account.admin_email)
        await client.put(
            "/llm-config",
            json={"endpoint_url": "https://example.invalid/v1", "model_name": "m"},
            headers=auth,
        )

        assert (await client.delete("/llm-config", headers=auth)).status_code in {200, 204}
        assert (await client.get("/llm-config", headers=auth)).json()["configured"] is False


class TestTheGatesOnTheRest:
    """Each of these was declared and never exercised through the API."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/users"),
            ("get", "/analytics/audit-events"),
            ("post", "/users/invite"),
        ],
    )
    async def test_a_member_is_refused(
        self, client: AsyncClient, account: Account, method: str, path: str
    ) -> None:
        auth = await headers(client, account.member_email)
        call = getattr(client, method)
        response = await (
            call(path, json={"email": "x@example.com"}, headers=auth)
            if method == "post"
            else call(path, headers=auth)
        )

        assert response.status_code == 403, f"{method.upper()} {path} admitted a member"


class TestAnalyticsIsScopedRatherThanGated:
    """`/analytics` admits a member on purpose, and that is the interesting part.

    It is gated on `query.history.own` *or* `query.history.any`, which `member` holds — so the
    refusal cannot be a 403. The protection is that the numbers are computed for the caller:
    `reads_all_history` comes from their permissions, not from the request. A gate would have
    been easier to get right and would have removed a screen the lowest-privileged user is
    entitled to.
    """

    async def test_a_member_sees_only_their_own_questions(
        self, client: AsyncClient, account: Account
    ) -> None:
        async with owner_session() as session:
            await session.execute(
                text(
                    "INSERT INTO queries (tenant_id, user_id, question, answer, model_used, "
                    "  latency_retrieval_ms, latency_generation_ms) "
                    "VALUES (:t, :u, 'what is my severance?', 'a', 'm', 1, 1)"
                ),
                {"t": account.tenant_id, "u": account.admin_id},
            )

        member = (
            await client.get("/analytics", headers=await headers(client, account.member_email))
        ).json()
        admin = (
            await client.get("/analytics", headers=await headers(client, account.admin_email))
        ).json()

        assert member["totals"]["queries"] == 0, "a member counted somebody else's question"
        assert admin["totals"]["queries"] == 1
