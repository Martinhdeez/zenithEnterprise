"""Strict-support status has the same meaning in buffered and SSE HTTP replies."""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.features.auth.router import router as auth_router
from app.features.generation.service import Answer, Streamed
from app.features.query import router as query_router
from conftest import PASSWORD, Account


@pytest.fixture
async def client(configured_engines: None) -> AsyncIterator[AsyncClient]:
    api = FastAPI()
    api.include_router(auth_router)
    api.include_router(query_router.router)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as http:
        yield http


async def test_strict_failure_is_explicit_and_no_draft_streams(
    client: AsyncClient, account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    safe = Answer(
        query_id=uuid4(),
        answer="I could not verify an answer against the available sources.",
        citations=[],
        abstained=True,
        consulted=[],
        model="local-test",
        degraded=True,
        reason="support_unavailable",
        took_retrieval_ms=1,
        took_generation_ms=2,
        support_status="not_assessed",
        took_support_ms=3,
    )

    class ScriptedService:
        def __init__(self, profile: object) -> None:
            pass

        async def answer(self, *args: object, **kwargs: object) -> Answer:
            return safe

        async def stream(self, *args: object, **kwargs: object) -> AsyncIterator[Streamed]:
            yield Streamed(token=safe.answer)
            yield Streamed(result=safe)

    monkeypatch.setattr(query_router, "AnswerService", ScriptedService)
    logged = await client.post(
        "/auth/login", json={"email": account.admin_email, "password": PASSWORD}
    )
    headers = {"Authorization": f"Bearer {logged.json()['access_token']}"}
    response = await client.post("/query", json={"question": "Public question?"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["support_status"] == "not_assessed"
    assert response.json()["took_support_ms"] == 3
    streamed = await client.post(
        "/query/stream", json={"question": "Public question?"}, headers=headers
    )
    assert streamed.status_code == 200
    assert "event: token" in streamed.text
    assert "event: result" in streamed.text
    assert "An unsupported draft" not in streamed.text
    assert "not_assessed" in streamed.text
