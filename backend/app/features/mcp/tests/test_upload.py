"""The trusted host streams through existing REST authorization and deduplication."""

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.common.exceptions import ZenithError
from app.core.database import owner_session
from app.features.auth.service import AuthService
from app.features.documents.router import router
from app.features.mcp.upload import local_api, selected_file, upload_selected
from app.main import handle_domain_error
from conftest import PASSWORD, Account


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.com:443",
        "http://localhost:8000",
        "http://127.0.0.1:8000/private",
        "http://token@127.0.0.1:8000",
        "http://127.0.0.1:8000?key=secret",
    ],
)
def test_external_or_credentialled_destination_is_rejected(origin: str) -> None:
    with pytest.raises(ValueError):
        local_api(origin)


def test_selected_root_and_file_limits(tmp_path: Path) -> None:
    (tmp_path / "selected.txt").write_text("Información pública", encoding="utf-8")
    with selected_file(tmp_path, Path("selected.txt")) as stream:
        assert stream.read().decode() == "Información pública"
    for path in (Path("../elsewhere.txt"), tmp_path / "selected.txt"):
        with pytest.raises(ValueError), selected_file(tmp_path, path):
            pytest.fail("invalid selection was opened")
    (tmp_path / "empty.txt").touch()
    with pytest.raises(ValueError), selected_file(tmp_path, Path("empty.txt")):
        pytest.fail("empty file was accepted")


def test_selected_link_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "public.txt"
    target.write_text("public")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable without Windows developer mode")
    with pytest.raises(ValueError), selected_file(tmp_path, Path("link.txt")):
        pytest.fail("a symlink was opened")


def test_file_growth_is_rejected_during_streaming(tmp_path: Path) -> None:
    path = tmp_path / "growing.txt"
    path.write_bytes(b"public")
    with selected_file(tmp_path, Path("growing.txt")) as stream:
        with path.open("ab") as writer:
            writer.write(b" unexpected growth")
        with pytest.raises(ValueError, match="grew"):
            stream.read()


async def test_binary_upload_dedupe_and_permission_rejection(
    account: Account, tmp_path: Path
) -> None:
    token = (await AuthService().authenticate(account.admin_email, PASSWORD)).access_token
    public = tmp_path / "public.txt"
    public.write_text("Protección de datos españoles: treinta días.", encoding="utf-8")
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(ZenithError, handle_domain_error)  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        first = await upload_selected(
            client, token, tmp_path, Path("public.txt"), [account.finance_label]
        )
        duplicate = await upload_selected(
            client, token, tmp_path, Path("public.txt"), [account.finance_label]
        )
        assert first["http_status"] == 201 and not first["deduplicated"]
        assert duplicate["http_status"] == 200 and duplicate["deduplicated"]
        assert first["document"]["id"] == duplicate["document"]["id"]  # type: ignore[index]
        assert first["document"]["status"] == "pending"  # type: ignore[index]
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
                client, token, tmp_path, Path("public.txt"), [account.finance_label]
            )
        assert denied.value.response.status_code == 403
