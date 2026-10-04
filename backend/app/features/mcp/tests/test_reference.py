"""Reference-host completion and citation gates, without running a model."""

from typing import cast
from uuid import uuid4

import httpx
import pytest
from mcp import Client
from mcp.types import CallToolResult

from app.features.mcp import reference
from app.features.retrieval.search import Hit


@pytest.mark.parametrize(
    ("finish", "answer", "source", "abstained"),
    [
        ("length", "Unfinished reasoning [1]", "public evidence", True),
        ("stop", "Treinta días [1]", "public evidence", False),
        ("stop", "Treinta días [1]", None, True),
        ("stop", "Treinta días [1]", "changed evidence", True),
        ("stop", "Uncited assertion", "public evidence", True),
    ],
)
async def test_completion_and_fresh_citation_gate(
    monkeypatch: pytest.MonkeyPatch,
    finish: str,
    answer: str,
    source: str | None,
    abstained: bool,
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [{"name": reference.LOCAL_MODEL, "digest": reference.LOCAL_DIGEST}]
                },
            )
        assert request.url.host == "127.0.0.1" and request.url.path == "/api/chat"
        return httpx.Response(200, json={"done_reason": finish, "message": {"content": answer}})

    actual_client = httpx.AsyncClient

    def model_client(**kwargs: object) -> httpx.AsyncClient:
        assert kwargs["trust_env"] is False
        return actual_client(
            base_url=reference.OLLAMA, transport=httpx.MockTransport(reply), trust_env=False
        )

    monkeypatch.setattr(reference.httpx, "AsyncClient", model_client)

    class Sources:
        calls = 0

        async def call_tool(self, name: str, arguments: dict[str, object]) -> CallToolResult:
            self.calls += 1
            assert name == "zenith_read_sources" and "source_ids" in arguments
            return CallToolResult(
                content=[],
                is_error=source is None,
                structured_content={
                    "sources": [
                        {"source_id": cast(list[str], arguments["source_ids"])[0], "text": source}
                    ]
                },
            )

    sources = Sources()
    hit = Hit(
        chunk_id=uuid4(),
        document_id=uuid4(),
        filename="public.txt",
        media_type="text/plain",
        page_num=None,
        char_start=0,
        char_end=15,
        text="public evidence",
        bboxes=[],
        lexical_rank=1,
        dense_rank=None,
        score=0,
    )
    result = await reference.local_answer(cast(Client, sources), "¿Qué plazo?", [hit])
    assert result["abstained"] is abstained
    if abstained:
        assert result["citations"] == []
    if finish != "stop":
        assert "Unfinished reasoning" not in str(result) and sources.calls == 0
