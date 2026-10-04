"""Real SDK calls, real application-role RLS, no model or paid credentials required."""

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import jwt
import pytest
from mcp import Client, StdioServerParameters
from mcp.types import TextContent
from sqlalchemy import text

from app.core.config import settings
from app.core.database import configure_engine, dispose_engines, owner_session
from app.core.hardware import PROFILES
from app.features.auth.service import AuthService
from app.features.mcp.server import create_server, local_lifespan
from app.features.mcp.service import LocalReads
from app.features.retrieval import service as retrieval
from app.features.retrieval.service import SearchService
from conftest import PASSWORD, Account, LexicalOnlyEmbedder


@pytest.fixture
async def public_source(account: Account, labelled_document: UUID) -> tuple[UUID, UUID]:
    async with owner_session() as session:
        await session.execute(
            text("UPDATE documents SET status='ready' WHERE id=:d"), {"d": labelled_document}
        )
        source = await session.scalar(
            text("SELECT id FROM chunks WHERE document_id=:d LIMIT 1"), {"d": labelled_document}
        )
        await session.execute(
            text("UPDATE chunks SET text=:body, char_start=100, char_end=144 WHERE id=:id"),
            {"id": source, "body": "Protección española: plazo de treinta días."},
        )
    assert isinstance(source, UUID)
    return labelled_document, source


@pytest.fixture
async def token(account: Account) -> str:
    return (await AuthService().authenticate(account.admin_email, PASSWORD)).access_token


@pytest.fixture
def client(token: str) -> Client:
    return Client(create_server(token))


async def test_startup_refuses_owner_role(token: str, owner_url: str, migrated: str) -> None:
    await dispose_engines()
    configure_engine(owner_url)
    try:
        with pytest.raises(RuntimeError, match="application database role"):
            async with local_lifespan(create_server(token)):
                pytest.fail("the owner role was permitted to serve")
    finally:
        await dispose_engines()
        configure_engine(migrated)


async def test_discovery_and_source_coordinates(
    client: Client, public_source: tuple[UUID, UUID]
) -> None:
    async with client:
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == {
            "zenith_search",
            "zenith_read_source",
            "zenith_get_document",
            "zenith_capabilities",
            "zenith_read_sources",
            "zenith_get_documents",
            "zenith_list_documents",
            "zenith_list_labels",
            "zenith_wait_documents",
        }
        assert client.protocol_version == "2026-07-28"
        assert [str(item.uri) for item in (await client.list_resources()).resources] == [
            "zenith://capabilities"
        ]
        result = await client.call_tool(
            "zenith_read_source", {"source_id": str(public_source[1]), "offset": 11, "length": 8}
        )
        assert not result.is_error
        value = result.structured_content
        assert value["char_start"] == 111 and value["char_end"] == 119
        assert value["text"] == "española" and value["truncated"]
        assert value["coordinate_unit"] == "page" and value["page_num"] in (1, 2)
        assert len(result.content) == 1 and isinstance(result.content[0], TextContent)
        assert "española" not in result.content[0].text  # no second large rendering


@pytest.mark.parametrize("length", [0, 8001, -1])
async def test_source_bounds(client: Client, public_source: tuple[UUID, UUID], length: int) -> None:
    async with client:
        result = await client.call_tool(
            "zenith_read_source", {"source_id": str(public_source[1]), "length": length}
        )
        assert result.is_error and result.structured_content is None


async def test_hidden_and_missing_have_same_error(
    account: Account, public_source: tuple[UUID, UUID]
) -> None:
    member = (await AuthService().authenticate(account.member_email, PASSWORD)).access_token
    async with Client(create_server(member)) as connected:
        errors: list[object] = []
        for document_id in (public_source[0], uuid4()):
            response = await connected.call_tool(
                "zenith_get_document", {"document_id": str(document_id)}
            )
            assert response.is_error
            errors.append(response.content)
        assert errors[0] == errors[1]
        response = await connected.call_tool(
            "zenith_read_source",
            {"source_id": str(public_source[1]), "tenant_id": str(account.tenant_id)},
        )
        assert response.is_error and response.structured_content is None


async def test_labels_revoked_after_first_read(
    account: Account, client: Client, public_source: tuple[UUID, UUID]
) -> None:
    async with client:
        first = await client.call_tool("zenith_read_source", {"source_id": str(public_source[1])})
        assert not first.is_error
        async with owner_session() as session:
            await session.execute(
                text("DELETE FROM role_labels WHERE label_id=:id"), {"id": account.finance_label}
            )
        second = await client.call_tool("zenith_read_source", {"source_id": str(public_source[1])})
        assert second.is_error and second.structured_content is None


async def test_direct_permission_guard(
    account: Account, token: str, public_source: tuple[UUID, UUID]
) -> None:
    from app.common.exceptions import PermissionDeniedError

    async with owner_session() as session:
        await session.execute(
            text(
                "DELETE FROM role_permissions WHERE permission_code='query.execute' "
                "AND role_id IN (SELECT id FROM roles WHERE tenant_id=:t)"
            ),
            {"t": account.tenant_id},
        )
    with pytest.raises(PermissionDeniedError):
        await LocalReads(token).source(public_source[1], 0, 100)
    async with Client(create_server(token)) as connected:
        response = await connected.call_tool("zenith_search", {"query": "española"})
        assert response.is_error and response.structured_content is None


@pytest.mark.parametrize("invalid", ["expired", "wrong_audience", "refresh", "revoked"])
async def test_invalid_credentials(
    invalid: str, account: Account, token: str, public_source: tuple[UUID, UUID]
) -> None:
    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    if invalid == "expired":
        payload["exp"] = datetime.now(UTC) - timedelta(seconds=1)
    elif invalid == "wrong_audience":
        payload["aud"] = "unrelated-network-resource"
    elif invalid == "refresh":
        payload["typ"] = "refresh"
    else:
        async with owner_session() as session:
            await session.execute(
                text("UPDATE users SET token_version=token_version+1 WHERE id=:u"),
                {"u": account.admin_id},
            )
    credential = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    async with Client(create_server(credential)) as connected:
        response = await connected.call_tool(
            "zenith_get_document", {"document_id": str(public_source[0])}
        )
        assert response.is_error and response.structured_content is None
        assert credential not in str(response.content)


async def test_search_matches_rest_service_and_rechecks_revocation(
    client: Client,
    token: str,
    account: Account,
    public_source: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with client:
        from app.features.mcp import service

        def lexical(profile: object) -> SearchService:
            assert isinstance(profile, retrieval.AccessProfile)
            return SearchService(profile, LexicalOnlyEmbedder(), PROFILES["low-spec"])  # type: ignore[arg-type]

        monkeypatch.setattr(service, "SearchService", lexical)
        profile = await LocalReads(token).profile()
        expected = await lexical(profile).search("española", 8)
        response = await client.call_tool("zenith_search", {"query": "española"})
        assert not response.is_error
        assert response.structured_content["degraded"] == expected.degraded
        assert [item["chunk_id"] for item in response.structured_content["hits"]] == [
            str(hit.chunk_id) for hit in expected.hits
        ]

        original = SearchService.search

        async def revoke(
            self: SearchService, *args: object, **kwargs: object
        ) -> retrieval.SearchResult:
            result = await original(self, "española", 8)
            async with owner_session() as session:
                await session.execute(
                    text("DELETE FROM role_labels WHERE label_id=:id"),
                    {"id": account.finance_label},
                )
            return result

        monkeypatch.setattr(SearchService, "search", revoke)
        second = await client.call_tool("zenith_search", {"query": "española"})
        assert not second.is_error and second.structured_content["hits"] == []
        assert second.structured_content["withheld_after_search"] > 0


async def test_cancellation_reaches_work(
    client: Client, monkeypatch: pytest.MonkeyPatch, public_source: tuple[UUID, UUID]
) -> None:
    async with client:
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def waiting(
            self: LocalReads, source_id: UUID, offset: int, length: int
        ) -> dict[str, object]:
            entered.set()
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()
            return {}

        monkeypatch.setattr(LocalReads, "source", waiting)
        task = asyncio.create_task(
            client.call_tool("zenith_read_source", {"source_id": str(public_source[1])})
        )
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(cancelled.wait(), 3)


async def test_actual_stdio_subprocess(
    token: str, migrated: str, public_source: tuple[UUID, UUID], tmp_path: Path
) -> None:
    # Forward only this test principal and the application-role DB URL, never owner access.
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.features.mcp.server"],
        cwd=str(Path(__file__).resolve().parents[4]),
        env={
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "PYTHONUTF8": "1",
            "ZENITH_JWT_SECRET": settings.jwt_secret,
            "ZENITH_DATABASE_URL": migrated,
            "ZENITH_MCP_ACCESS_TOKEN": token,
            "ZENITH_STORAGE_DIR": str(tmp_path),
        },
    )
    # Match the reference host's 35-second budget, including the 30-second startup guard.
    async with Client(parameters, read_timeout_seconds=35) as connected:
        assert connected.protocol_version == "2026-07-28"
        result = await connected.call_tool(
            "zenith_read_source", {"source_id": str(public_source[1])}
        )
        assert not result.is_error
        assert result.structured_content["source_id"] == str(public_source[1])
        assert token not in str(result.content)


@pytest.mark.skipif(
    not os.environ.get("ZENITH_RUN_MCP_MODEL_PROOF"),
    reason="opt-in pinned cached local-model proof",
)
async def test_local_model_cites_seeded_public_source(
    client: Client, public_source: tuple[UUID, UUID]
) -> None:
    import json
    import time

    from app.features.mcp.reference import local_answer
    from app.features.retrieval.search import Hit

    async with client:
        source = await client.call_tool("zenith_read_source", {"source_id": str(public_source[1])})
        assert not source.is_error
        body = source.structured_content
        hit = Hit(
            chunk_id=public_source[1],
            document_id=public_source[0],
            filename=body["filename"],
            media_type=body["media_type"],
            page_num=body["page_num"],
            char_start=body["char_start"],
            char_end=body["char_end"],
            text=body["text"],
            bboxes=body["bboxes"],
            lexical_rank=None,
            dense_rank=None,
            score=0,
        )
        started = time.monotonic()
        result = await local_answer(client, "¿Cuál es el plazo de protección?", [hit])
        record = {
            "fixture_ready_origin": "seeded public source; not an ingestion proof",
            "seconds": time.monotonic() - started,
            "result": result,
        }
        output = Path(os.environ["ZENITH_MCP_MODEL_OUTPUT"])
        output.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        assert not result["abstained"]
        assert result["citations"] and result["fabricated_markers"] == 0
        assert result["finish_reason"] == "stop"
        assert "treinta" in str(result["answer"]).lower()
