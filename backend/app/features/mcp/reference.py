"""Bounded local reference client: search, source read and optional local answer.

This is a fixed workflow, not an autonomous agent or a connector to arbitrary servers.
Uploads are performed by the separate trusted host component in `upload.py`.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

import httpx
from mcp import Client, StdioServerParameters

from app.features.generation.answering.citations import bind
from app.features.generation.answering.prompt import ABSTENTION, SYSTEM, build
from app.features.retrieval.schemas import HitResponse
from app.features.retrieval.search import Hit

LOCAL_MODEL = "llama3.2:3b"
LOCAL_DIGEST = "a80c4f17acd55265feec403c7aef86be0c25983ab279d83f3bcd3abbcb5b8b72"
OLLAMA = "http://127.0.0.1:11434"


async def evidence(client: Client, query: str, document_id: UUID | None) -> list[Hit]:
    arguments: dict[str, object] = {"query": query, "limit": 3}
    if document_id is not None:
        arguments["documents"] = [str(document_id)]
    result = await client.call_tool("zenith_search", arguments)
    if result.is_error:
        raise RuntimeError("search was refused")
    selected = [HitResponse.model_validate(item) for item in result.structured_content["hits"]]
    if not selected:
        return []
    response = await client.call_tool(
        "zenith_read_sources",
        {"source_ids": [str(hit.chunk_id) for hit in selected], "length": 1000},
    )
    if response.is_error:
        raise RuntimeError("source batch was refused")
    sources = {item["source_id"]: item for item in response.structured_content["sources"]}
    hits: list[Hit] = []
    for hit in selected:
        source = sources.get(str(hit.chunk_id))
        if source is None:
            continue
        hit.text = source["text"]
        hit.char_start, hit.char_end = source["char_start"], source["char_end"]
        hit.filename = source["filename"]
        hits.append(Hit(**hit.model_dump()))
    return hits


async def local_answer(client: Client, query: str, hits: list[Hit]) -> dict[str, object]:
    if not hits:
        return {"answer": ABSTENTION, "citations": [], "abstained": True}
    async with httpx.AsyncClient(base_url=OLLAMA, timeout=90, trust_env=False) as model:
        tags = await model.get("/api/tags")
        tags.raise_for_status()
        if not any(
            tag["name"] == LOCAL_MODEL and tag["digest"] == LOCAL_DIGEST
            for tag in tags.json()["models"]
        ):
            raise RuntimeError("the pinned local model is not cached; no download was attempted")
        response = await model.post(
            "/api/chat",
            json={
                "model": LOCAL_MODEL,
                "stream": False,
                "keep_alive": 0,
                "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 256},
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": build(query, hits)},
                ],
            },
        )
        response.raise_for_status()
        body = response.json()
    if body.get("done_reason") != "stop":
        return {
            "answer": ABSTENTION,
            "citations": [],
            "abstained": True,
            "withheld": "generation did not finish normally",
            "finish_reason": body.get("done_reason"),
        }
    bound = bind(body["message"]["content"], hits)
    # Reauthorize citations after generation too. A retained source handle is not a grant.
    if bound.citations:
        identifiers = list(dict.fromkeys(str(citation.chunk_id) for citation in bound.citations))
        checked = await client.call_tool(
            "zenith_read_sources", {"source_ids": identifiers, "length": 1000}
        )
        sources = (
            {item["source_id"]: item for item in checked.structured_content["sources"]}
            if not checked.is_error
            else {}
        )
        if any(
            sources.get(str(citation.chunk_id), {}).get("text") != citation.text
            for citation in bound.citations
        ):
            return {
                "answer": ABSTENTION,
                "citations": [],
                "abstained": True,
                "withheld": "source access or contents changed after generation",
            }
    return {
        "answer": bound.answer,
        "abstained": bound.abstained,
        "fabricated_markers": bound.fabricated,
        "citations": [
            {
                "marker": cite.marker,
                "source_id": str(cite.chunk_id),
                "document_id": str(cite.document_id),
                "page_num": cite.page_num,
                "char_start": cite.char_start,
                "char_end": cite.char_end,
            }
            for cite in bound.citations
        ],
        "model": LOCAL_MODEL,
        "model_digest": LOCAL_DIGEST,
        "model_endpoint": OLLAMA,
        "finish_reason": body["done_reason"],
        "model_total_seconds": body.get("total_duration", 0) / 1_000_000_000,
        "citation_validation": "reference identity/range only; not semantic entailment",
    }


def main() -> None:
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True)
    parser.add_argument("--document", type=UUID)
    parser.add_argument("--answer-local", action="store_true")
    args = parser.parse_args()
    # The stdio child gets only its local app configuration. No owner/platform URL,
    # external provider key, cloud API key or tracing configuration is forwarded.
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "PYTHONUTF8",
        "ZENITH_JWT_SECRET",
        "ZENITH_DATABASE_URL",
        "ZENITH_MCP_ACCESS_TOKEN",
        "ZENITH_STORAGE_DIR",
        "ZENITH_TEI_EMBED_URL",
        "ZENITH_TEI_RERANK_URL",
        "ZENITH_HARDWARE",
    }
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.features.mcp.server"],
        cwd=Path(__file__).resolve().parents[3],
        env={name: value for name, value in os.environ.items() if name in allowed},
    )

    async def run() -> None:
        async with Client(parameters, read_timeout_seconds=35) as client:
            hits = await evidence(client, args.query, args.document)
            result = (
                await local_answer(client, args.query, hits)
                if args.answer_local
                else {
                    "source_ids": [str(hit.chunk_id) for hit in hits],
                    "protocol": client.protocol_version,
                }
            )
            print(json.dumps(result, ensure_ascii=False))

    asyncio.run(run())


if __name__ == "__main__":
    main()
