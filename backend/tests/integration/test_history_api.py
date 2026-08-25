"""Query history over real HTTP, and the one property it exists to protect.

F15 shipped this as a `WHERE` clause in Python and said so loudly, because it was the only
place in the system where application code did security work. Migration 0005 moved it into a
policy:

    tenant_id = zenith_current_tenant()
    AND (zenith_reads_all_history() OR user_id = zenith_current_user_id())

which is stronger and shifts the failure. The policy cannot be forgotten; the *binding* can.
`AccessProfile.context` leaves `user_id` unset — the same omission that left `bind_tenant`
doing nothing for months — and a route that fails to bind it reads `user_id = NULL`, which is
never true, so it returns nothing rather than everything. Safe, and still wrong.

The dangerous direction is the other one: binding `reads_all_history` from anything but the
caller's own permissions.
"""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.common.exceptions import ZenithError
from app.core.database import owner_session
from app.features.auth.router import router as auth_router
from app.features.query.router import router as query_router
from app.main import handle_domain_error
from conftest import PASSWORD, Account

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client(configured_engines: None) -> AsyncIterator[AsyncClient]:
    api = FastAPI()
    api.add_exception_handler(ZenithError, handle_domain_error)  # type: ignore[arg-type]
    api.include_router(auth_router)
    api.include_router(query_router)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as http:
        yield http


async def headers(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def ask(account: Account, user_id: object, question: str) -> None:
    async with owner_session() as session:
        await session.execute(
            text(
                "INSERT INTO queries (tenant_id, user_id, question, answer, model_used, "
                "  latency_retrieval_ms, latency_generation_ms) "
                "VALUES (:t, :u, :q, 'an answer', 'm', 1, 1)"
            ),
            {"t": account.tenant_id, "u": user_id, "q": question},
        )


async def test_a_member_sees_only_their_own_questions(
    client: AsyncClient, account: Account
) -> None:
    """The questions people ask are more revealing than the documents they read — *"what is my
    severance?"*, *"can I be dismissed for this?"* — which is what made this worth a third
    context variable rather than a filter."""
    await ask(account, account.admin_id, "can I be dismissed for this?")
    await ask(account, account.member_id, "how much holiday do I have?")

    body = (
        await client.get("/query/history", headers=await headers(client, account.member_email))
    ).json()

    questions = [entry["question"] for entry in body["entries"]]
    assert questions == ["how much holiday do I have?"]


async def test_a_holder_of_history_any_sees_the_organisation(
    client: AsyncClient, account: Account
) -> None:
    """`admin` holds `query.history.any`, and the whole point of the distinction is that
    somebody does.

    There is no "show me everyone's" parameter to refuse: the permission decides the default
    set, and `mine=true` narrows it. That is the right way round — a flag that widened would
    have to be checked, and a flag that only narrows cannot be got wrong.
    """
    await ask(account, account.admin_id, "can I be dismissed for this?")
    await ask(account, account.member_id, "how much holiday do I have?")

    body = (
        await client.get("/query/history", headers=await headers(client, account.admin_email))
    ).json()

    assert len(body["entries"]) == 2


async def test_mine_narrows_even_for_somebody_who_may_read_everything(
    client: AsyncClient, account: Account
) -> None:
    """The parameter can never widen, only narrow — which is why it needs no permission of
    its own."""
    await ask(account, account.admin_id, "can I be dismissed for this?")
    await ask(account, account.member_id, "how much holiday do I have?")

    body = (
        await client.get(
            "/query/history?mine=true", headers=await headers(client, account.admin_email)
        )
    ).json()

    assert [entry["question"] for entry in body["entries"]] == ["can I be dismissed for this?"]
