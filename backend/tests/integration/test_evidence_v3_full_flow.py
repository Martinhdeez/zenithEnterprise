"""A public upload through ready ingestion, scoped answer, source, and history."""

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.common.exceptions import ZenithError
from app.core.config import settings
from app.features.auth.router import router as auth_router
from app.features.auth.service import AccessProfile
from app.features.documents.router import router as documents_router
from app.features.documents.storage import DocumentStorage
from app.features.generation.adapters.mock import MockProvider
from app.features.generation.service import AnswerService
from app.features.ingestion.pipeline import IngestionPipeline
from app.features.ingestion.tests.test_pipeline import StubEmbedder
from app.features.query import router as query_router
from app.features.retrieval.service import SearchService
from app.features.tenancy.context import TenantContext
from app.main import handle_domain_error
from conftest import PASSWORD, Account, WorkingEmbedder


@pytest.fixture
async def client(configured_engines: None) -> AsyncIterator[AsyncClient]:
    api = FastAPI()
    api.add_exception_handler(ZenithError, handle_domain_error)  # type: ignore[arg-type]
    api.include_router(auth_router)
    api.include_router(documents_router)
    api.include_router(query_router.router)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as http:
        yield http


async def test_public_text_upload_ready_scoped_answer_source_and_history(
    client: AsyncClient, account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    login = await client.post(
        "/auth/login", json={"email": account.admin_email, "password": PASSWORD}
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    original = ("Residents pay 5%. Students pay 0%.\n" * 8).encode()
    uploaded = await client.post(
        "/documents",
        files={"file": ("public-rates.txt", original, "text/plain")},
        headers=headers,
    )
    assert uploaded.status_code == 201
    receipt = uploaded.json()
    document_id = UUID(receipt["document"]["id"])
    assert receipt["document"]["status"] == "pending"
    context = TenantContext.for_tenant(
        account.tenant_id, [UUID(value) for value in receipt["labels"]]
    )
    ingested = await IngestionPipeline(
        context,
        DocumentStorage(root=settings.storage_dir),
        StubEmbedder(),  # type: ignore[arg-type]
    ).run(document_id)
    assert ingested.status == "ready" and ingested.chunks > 0

    model = MockProvider(["Students pay 0% [1]."])

    def scripted_service(profile: AccessProfile) -> AnswerService:
        return AnswerService(
            profile,
            provider=model,
            search=SearchService(profile, embedder=WorkingEmbedder()),  # type: ignore[arg-type]
        )

    monkeypatch.setattr(query_router, "AnswerService", scripted_service)
    answer = await client.post(
        "/query",
        json={"question": "What do students pay?", "documents": [str(document_id)]},
        headers=headers,
    )
    assert answer.status_code == 200
    body = answer.json()
    assert not body["abstained"] and body["answer"] == "Students pay 0% [1]."
    assert body["citations"] and body["citations"][0]["document_id"] == str(document_id)
    assert "Students pay 0%" in body["citations"][0]["text"]
    assert body["support_status"] is None  # the default legacy path remains enabled

    source = await client.get(f"/documents/{document_id}/file", headers=headers)
    assert source.status_code == 200 and source.content == original
    history = await client.get("/query/history", headers=headers)
    assert history.status_code == 200
    assert any(item["query_id"] == body["query_id"] for item in history.json()["entries"])
