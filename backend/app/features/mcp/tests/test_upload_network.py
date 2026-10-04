"""Actual loopback TCP and uploader subprocess, with real application-role REST."""

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import text

from app.common.exceptions import ZenithError
from app.core.database import owner_session
from app.features.auth.service import AuthService
from app.features.documents.router import router
from app.features.ingestion.tests.test_pipeline import pdf_bytes
from app.features.mcp.upload import upload_selected
from app.main import handle_domain_error
from conftest import PASSWORD, Account


async def test_selected_binary_upload_over_tcp_and_cli(
    account: Account,
    tmp_path: Path,
) -> None:
    token = (await AuthService().authenticate(account.admin_email, PASSWORD)).access_token
    content = pdf_bytes(["Spanish public fixture: plazo de treinta dias."])
    (tmp_path / "selected.pdf").write_bytes(content)
    api = FastAPI()
    api.include_router(router)
    api.add_exception_handler(ZenithError, handle_domain_error)  # type: ignore[arg-type]
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(api, log_level="error", access_log=False))
    worker = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(15):
            while not server.started:
                if worker.done():
                    await worker
                    pytest.fail("network API exited before startup")
                await asyncio.sleep(0.01)
        environment = {
            name: value
            for name, value in os.environ.items()
            if name
            in {
                "PATH",
                "SYSTEMROOT",
                "WINDIR",
                "PYTHONPATH",
                "VIRTUAL_ENV",
                "TEMP",
                "TMP",
                "PYTHONUTF8",
            }
        }
        environment["ZENITH_MCP_ACCESS_TOKEN"] = token
        # The Windows Selector loop supports psycopg, but not asyncio subprocesses.
        process = await asyncio.to_thread(
            subprocess.Popen,
            [
                sys.executable,
                "-m",
                "app.features.mcp.upload",
                "--api",
                f"http://127.0.0.1:{port}",
                "--root",
                str(tmp_path),
                "--file",
                "selected.pdf",
                "--label",
                str(account.finance_label),
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.to_thread(process.communicate, timeout=30)
        except BaseException:
            process.kill()
            await asyncio.to_thread(process.wait)
            raise
        assert process.returncode == 0, stderr.decode()
        first = json.loads(stdout)
        assert first["http_status"] == 201 and not first["deduplicated"]
        assert first["document"]["status"] == "pending"
        assert token.encode() not in stdout + stderr
        identifier = first["document"]["id"]
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            trust_env=False,
            timeout=15,
        ) as client:
            duplicate = await upload_selected(
                client,
                token,
                tmp_path,
                Path("selected.pdf"),
                [account.finance_label],
            )
            assert duplicate["http_status"] == 200 and duplicate["deduplicated"]
            assert duplicate["document"]["id"] == identifier  # type: ignore[index]
            original = await client.get(
                f"/documents/{identifier}/file",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert original.status_code == 200
            assert hashlib.sha256(original.content).digest() == hashlib.sha256(content).digest()
            assert (await client.get(f"/documents/{identifier}/file")).status_code == 401
            async with owner_session() as session:
                await session.execute(
                    text(
                        "DELETE FROM role_permissions WHERE permission_code='documents.upload' "
                        "AND role_id IN (SELECT id FROM roles WHERE tenant_id=:t)"
                    ),
                    {"t": account.tenant_id},
                )
            with pytest.raises(httpx.HTTPStatusError) as denied:
                await upload_selected(
                    client,
                    token,
                    tmp_path,
                    Path("selected.pdf"),
                    [account.finance_label],
                )
            assert denied.value.response.status_code == 403
    finally:
        server.should_exit = True
        try:
            async with asyncio.timeout(10):
                await worker
        except TimeoutError:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        listener.close()
