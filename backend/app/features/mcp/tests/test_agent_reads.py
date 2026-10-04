"""Agent discovery, batch reads and waits through the real SDK and application-role RLS."""

import asyncio
import json
import os
import statistics
import time
from pathlib import Path
from typing import TypedDict, cast
from uuid import UUID, uuid4

import pytest
from mcp import Client
from sqlalchemy import event, text

from app.core.database import get_session_factory, owner_session
from app.features.auth.service import AuthService
from app.features.mcp.server import create_server
from app.features.mcp.service import LocalReads
from app.features.mcp.tests.test_local_reads import client as client
from app.features.mcp.tests.test_local_reads import public_source as public_source
from app.features.mcp.tests.test_local_reads import token as token
from conftest import PASSWORD, Account


class BatchTrial(TypedDict):
    trial: int
    mode: str
    seconds: float
    sql_calls: int


class BatchSummary(TypedDict):
    median_seconds: float
    sql_calls: list[int]


async def test_ordered_batch_matches_single_reads_and_hides_inaccessible_ids(
    client: Client, account: Account, public_source: tuple[UUID, UUID]
) -> None:
    source_id = public_source[1]
    missing = uuid4()
    async with client:
        single = await client.call_tool(
            "zenith_read_source", {"source_id": str(source_id), "length": 8}
        )
        batch = await client.call_tool(
            "zenith_read_sources", {"source_ids": [str(missing), str(source_id)], "length": 8}
        )
        assert not batch.is_error
        assert batch.structured_content["sources"] == [single.structured_content]
        assert batch.structured_content["unavailable_source_ids"] == [str(missing)]
        async with owner_session() as session:
            await session.execute(
                text("DELETE FROM role_labels WHERE label_id=:id"), {"id": account.finance_label}
            )
        revoked = await client.call_tool(
            "zenith_read_sources", {"source_ids": [str(source_id), str(missing)], "length": 8}
        )
        assert not revoked.is_error and revoked.structured_content["sources"] == []
        assert set(revoked.structured_content["unavailable_source_ids"]) == {
            str(source_id),
            str(missing),
        }


@pytest.mark.parametrize("count,length", [(9, 1), (8, 2001), (1, 8001), (0, 100)])
async def test_batch_output_limits(client: Client, count: int, length: int) -> None:
    async with client:
        response = await client.call_tool(
            "zenith_read_sources",
            {"source_ids": [str(uuid4()) for _ in range(count)], "length": length},
        )
        assert response.is_error and response.structured_content is None


async def test_duplicate_sources_refused(client: Client, public_source: tuple[UUID, UUID]) -> None:
    async with client:
        response = await client.call_tool(
            "zenith_read_sources", {"source_ids": [str(public_source[1])] * 2}
        )
        assert response.is_error


async def test_paginated_discovery_reuses_metadata_policy_and_bounds(
    account: Account, token: str, public_source: tuple[UUID, UUID]
) -> None:
    reads = LocalReads(token)
    ids: list[str] = []
    cursor = None
    while True:
        result = await reads.document_page(1, cursor, None, None, None)
        rows = cast(list[dict[str, object]], result["documents"])
        assert len(rows) <= 1
        ids.extend(str(item["id"]) for item in rows)
        assert all("status_detail" not in item for item in rows)
        cursor = result["next_cursor"]
        assert cursor is None or isinstance(cursor, str)
        if cursor is None:
            break
    assert str(public_source[0]) in ids and len(ids) == len(set(ids))
    member = (await AuthService().authenticate(account.member_email, PASSWORD)).access_token
    hidden = await LocalReads(member).document_page(50, None, "ready", account.finance_label, None)
    assert hidden["documents"] == []
    async with Client(create_server(member)) as connected:
        labels = await connected.call_tool("zenith_list_labels", {})
        assert not labels.is_error
        assert str(account.finance_label) not in str(labels.structured_content)
        assert str(account.hr_label) not in str(labels.structured_content)
        assert str(account.default_label) in str(labels.structured_content)
        batch = await connected.call_tool(
            "zenith_get_documents", {"document_ids": [str(public_source[0]), str(uuid4())]}
        )
        assert batch.structured_content["documents"] == []
    with pytest.raises(ValueError):
        await reads.document_page(51, None, None, None, None)


async def test_wait_readiness_timeout_failure_and_revocation(
    client: Client,
    account: Account,
    public_source: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id = public_source[0]
    async with client:
        ready = await client.call_tool(
            "zenith_wait_documents", {"document_ids": [str(document_id)]}
        )
        assert ready.structured_content["all_ready"] and not ready.structured_content["timed_out"]
        async with owner_session() as session:
            await session.execute(
                text("UPDATE documents SET status='embedding' WHERE id=:d"), {"d": document_id}
            )
        timeout = await client.call_tool(
            "zenith_wait_documents", {"document_ids": [str(document_id)], "timeout_seconds": 0}
        )
        assert (
            timeout.structured_content["timed_out"] and not timeout.structured_content["all_ready"]
        )
        first_snapshot = asyncio.Event()
        original = LocalReads.documents

        async def observed(self: LocalReads, ids: list[UUID]) -> dict[str, object]:
            result = await original(self, ids)
            first_snapshot.set()
            return result

        monkeypatch.setattr(LocalReads, "documents", observed)
        waiting = asyncio.create_task(
            client.call_tool(
                "zenith_wait_documents", {"document_ids": [str(document_id)], "timeout_seconds": 5}
            )
        )
        await asyncio.wait_for(first_snapshot.wait(), timeout=10)
        async with owner_session() as session:
            await session.execute(
                text("UPDATE documents SET status='failed' WHERE id=:d"), {"d": document_id}
            )
            await session.execute(
                text("DELETE FROM role_labels WHERE label_id=:l"), {"l": account.finance_label}
            )
        revoked = await waiting
        assert revoked.structured_content["documents"] == []
        assert revoked.structured_content["unavailable_document_ids"] == [str(document_id)]
        assert not revoked.structured_content["all_ready"]


async def test_capabilities_resource_and_prompt_require_current_authority(
    client: Client, token: str, account: Account
) -> None:
    async with client:
        capabilities = await client.call_tool("zenith_capabilities", {})
        assert capabilities.structured_content["processing"] == "local"
        resources = await client.read_resource("zenith://capabilities")
        assert "source_batch_chars" in str(resources)
        assert [prompt.name for prompt in (await client.list_prompts()).prompts] == [
            "zenith_cited_answer"
        ]
        prompt = await client.get_prompt("zenith_cited_answer", {"question": "¿Qué fuentes hay?"})
        assert "¿Qué fuentes hay?" in str(prompt)
        async with owner_session() as session:
            await session.execute(
                text("UPDATE users SET token_version=token_version+1 WHERE id=:u"),
                {"u": account.admin_id},
            )
        revoked = await client.call_tool("zenith_capabilities", {})
        assert revoked.is_error and token not in str(revoked)


@pytest.mark.skipif(
    not os.environ.get("ZENITH_RUN_MCP_BATCH_BENCHMARK"), reason="opt-in SDK/DB latency measurement"
)
async def test_batch_read_benchmark(client: Client, public_source: tuple[UUID, UUID]) -> None:
    source_ids = [public_source[1]] + [uuid4() for _ in range(7)]
    async with owner_session() as session:
        for source_id in source_ids[1:]:
            await session.execute(
                text(
                    "INSERT INTO chunks(id, tenant_id, document_id, page_num, "
                    "char_start, char_end, text, bboxes) "
                    "SELECT :new, tenant_id, document_id, page_num, "
                    "char_start, char_end, text, bboxes "
                    "FROM chunks WHERE id=:old"
                ),
                {"new": source_id, "old": public_source[1]},
            )
    engine = get_session_factory().kw["bind"].sync_engine
    count = 0

    def counted(*args: object) -> None:
        nonlocal count
        count += 1

    event.listen(engine, "after_cursor_execute", counted)
    trials: list[BatchTrial] = []
    try:
        async with client:
            for trial in range(16):
                order = ("single", "batch") if trial % 2 else ("batch", "single")
                contents: dict[str, object] = {}
                for mode in order:
                    before, first_count = time.perf_counter(), count
                    if mode == "single":
                        results = [
                            await client.call_tool(
                                "zenith_read_source", {"source_id": str(i), "length": 1000}
                            )
                            for i in source_ids
                        ]
                        assert all(not result.is_error for result in results)
                        contents[mode] = [result.structured_content for result in results]
                    else:
                        result = await client.call_tool(
                            "zenith_read_sources",
                            {"source_ids": list(map(str, source_ids)), "length": 1000},
                        )
                        assert not result.is_error
                        contents[mode] = result.structured_content["sources"]
                    if trial:
                        trials.append(
                            {
                                "trial": trial,
                                "mode": mode,
                                "seconds": time.perf_counter() - before,
                                "sql_calls": count - first_count,
                            }
                        )
                assert contents["single"] == contents["batch"]
    finally:
        event.remove(engine, "after_cursor_execute", counted)
    report: dict[str, BatchSummary] = {
        mode: {
            "median_seconds": statistics.median(
                float(row["seconds"]) for row in trials if row["mode"] == mode
            ),
            "sql_calls": sorted(
                set(int(row["sql_calls"]) for row in trials if row["mode"] == mode)
            ),
        }
        for mode in ("single", "batch")
    }
    assert report["batch"]["sql_calls"][0] * 8 == report["single"]["sql_calls"][0]
    Path(os.environ["ZENITH_MCP_BATCH_OUTPUT"]).write_text(
        json.dumps(
            {"sdk_transport": "in-process", "sources": 8, "trials": trials, "summary": report},
            indent=2,
        ),
        encoding="utf-8",
    )
