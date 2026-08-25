"""The document endpoints over real HTTP, against a real Postgres with RLS active.

The service tests cover the decisions. These cover the surface: the status code that tells
a client whether anything was stored, the permission gates being in the signatures rather
than in the bodies, and the two places where an error message could disclose a document
the caller is not allowed to know about.
"""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.common.exceptions import ZenithError
from app.core.database import owner_session
from app.features.auth.router import router as auth_router
from app.features.documents.router import router as documents_router
from app.main import handle_domain_error
from conftest import PASSWORD, Account

pytestmark = pytest.mark.asyncio

PDF = b"%PDF-1.7\nnot a real document, but it starts like one\n"


@pytest.fixture
async def client(configured_engines: None) -> AsyncIterator[AsyncClient]:
    api = FastAPI()
    api.add_exception_handler(ZenithError, handle_domain_error)  # type: ignore[arg-type]
    api.include_router(auth_router)
    api.include_router(documents_router)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as http:
        yield http


async def token(client: AsyncClient, email: str) -> str:
    response = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return str(response.json()["access_token"])


async def headers(client: AsyncClient, email: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {await token(client, email)}"}


async def test_upload_returns_201_and_the_document(client: AsyncClient, account: Account) -> None:
    response = await client.post(
        "/documents",
        files={"file": ("report.pdf", PDF, "application/pdf")},
        headers=await headers(client, account.admin_email),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["deduplicated"] is False
    assert body["document"]["filename"] == "report.pdf"
    assert body["document"]["status"] == "pending"
    # Quarantine, not the tenant default. An upload that named no compartment is unfiled, and
    # since 0017 unfiled means "readable by an administrator and by whoever sent it" instead
    # of "readable by everybody" for the length of the ingestion.
    assert body["labels"] == [str(account.quarantine_label)]


async def test_a_duplicate_returns_200_not_201(client: AsyncClient, account: Account) -> None:
    """The status code has to distinguish them.

    A client treating both as "created" would tell the user a new document exists when
    nothing was stored — and would never surface that the labels of an existing document
    just changed.
    """
    auth = await headers(client, account.admin_email)
    await client.post("/documents", files={"file": ("a.pdf", PDF, "application/pdf")}, headers=auth)

    response = await client.post(
        "/documents", files={"file": ("b.pdf", PDF, "application/pdf")}, headers=auth
    )

    assert response.status_code == 200
    assert response.json()["deduplicated"] is True


async def test_uploading_requires_the_permission(client: AsyncClient, account: Account) -> None:
    """`member` holds neither `documents.upload` nor any delete permission."""
    response = await client.post(
        "/documents",
        files={"file": ("report.pdf", PDF, "application/pdf")},
        headers=await headers(client, account.member_email),
    )

    assert response.status_code == 403


async def test_a_non_pdf_is_rejected_with_415(client: AsyncClient, account: Account) -> None:
    response = await client.post(
        "/documents",
        files={"file": ("virus.pdf", b"MZ\x90\x00 an executable", "application/pdf")},
        headers=await headers(client, account.admin_email),
    )

    assert response.status_code == 415


async def test_an_announced_oversized_body_is_refused_before_it_is_read(
    client: AsyncClient, account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "max_file_bytes", 8)

    response = await client.post(
        "/documents",
        files={"file": ("big.pdf", PDF, "application/pdf")},
        headers=await headers(client, account.admin_email),
    )

    assert response.status_code == 413


async def test_the_list_shows_only_what_the_caller_reaches(
    client: AsyncClient, account: Account
) -> None:
    """The member reaches the default label; the Finance document is not theirs to see.

    Neither is the admin's unlabelled upload, and that is migration 0017 working rather than
    an omission. Before it, a document nobody had classified carried the tenant default —
    granted to `member` as well as `admin` — so it was readable by the whole tenant from the
    moment the upload answered until the classifier ran at the end of ingestion. It now waits
    in the quarantine label, which only `admin` reaches.
    """
    admin = await headers(client, account.admin_email)
    await client.post(
        "/documents",
        files={"file": ("general.pdf", PDF, "application/pdf")},
        headers=admin,
    )
    async with owner_session() as session:
        document_id = await session.scalar(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes) "
                "VALUES (:t, 'finance.pdf', :sha, 10) RETURNING id"
            ),
            {"t": account.tenant_id, "sha": str(uuid4())},
        )
        await session.execute(
            text("INSERT INTO document_labels (document_id, label_id) VALUES (:d, :l)"),
            {"d": document_id, "l": account.finance_label},
        )

    member = await client.get("/documents", headers=await headers(client, account.member_email))
    assert [document["filename"] for document in member.json()["items"]] == []

    # The admin sees the quarantined upload — somebody has to be able to file it — and still
    # not the Finance document, which nothing about quarantine changes.
    listed = await client.get("/documents", headers=admin)
    assert [document["filename"] for document in listed.json()["items"]] == [
        "finance.pdf",
        "general.pdf",
    ]


async def test_an_unreachable_document_is_404_not_403(
    client: AsyncClient, account: Account
) -> None:
    """A 403 would confirm it exists, which is what mvp.md 2.2 forbids."""
    async with owner_session() as session:
        document_id = await session.scalar(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes) "
                "VALUES (:t, 'finance.pdf', :sha, 10) RETURNING id"
            ),
            {"t": account.tenant_id, "sha": str(uuid4())},
        )
        await session.execute(
            text("INSERT INTO document_labels (document_id, label_id) VALUES (:d, :l)"),
            {"d": document_id, "l": account.finance_label},
        )

    member = await headers(client, account.member_email)

    assert (await client.get(f"/documents/{document_id}", headers=member)).status_code == 404


async def test_download_streams_the_original_bytes(client: AsyncClient, account: Account) -> None:
    auth = await headers(client, account.admin_email)
    uploaded = await client.post(
        "/documents", files={"file": ("report.pdf", PDF, "application/pdf")}, headers=auth
    )
    document_id = uploaded.json()["document"]["id"]

    response = await client.get(f"/documents/{document_id}/file", headers=auth)

    assert response.status_code == 200
    assert response.content == PDF
    assert response.headers["content-type"] == "application/pdf"


async def test_deletion_removes_it_from_the_list(client: AsyncClient, account: Account) -> None:
    auth = await headers(client, account.admin_email)
    uploaded = await client.post(
        "/documents", files={"file": ("report.pdf", PDF, "application/pdf")}, headers=auth
    )
    document_id = uploaded.json()["document"]["id"]

    deleted = await client.delete(f"/documents/{document_id}", headers=auth)

    assert deleted.status_code == 204
    assert (await client.get("/documents", headers=auth)).json()["items"] == []


async def test_the_listing_is_paginated_over_http(client: AsyncClient, account: Account) -> None:
    """The wire contract: an object with `items` and `next_cursor`, never a bare array.

    The shape matters more than the numbers. A flat array is the thing that becomes
    impossible to change once a frontend depends on it, which is exactly why pagination
    lands with the endpoint rather than after it.
    """
    auth = await headers(client, account.admin_email)
    for index in range(3):
        await client.post(
            "/documents",
            files={"file": (f"doc-{index}.pdf", PDF + bytes([index]), "application/pdf")},
            headers=auth,
        )

    first = await client.get("/documents?limit=2", headers=auth)
    body = first.json()
    second = await client.get(f"/documents?limit=2&cursor={body['next_cursor']}", headers=auth)

    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None
    assert len(second.json()["items"]) == 1
    assert second.json()["next_cursor"] is None


async def test_a_tampered_cursor_is_a_400(client: AsyncClient, account: Account) -> None:
    response = await client.get(
        "/documents?cursor=bm90LWEtY3Vyc29y", headers=await headers(client, account.admin_email)
    )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_cursor"


async def test_the_limit_is_capped_at_the_boundary(client: AsyncClient, account: Account) -> None:
    """FastAPI rejects it before the handler runs, so the ceiling is in the schema too."""
    response = await client.get(
        "/documents?limit=100000", headers=await headers(client, account.admin_email)
    )

    assert response.status_code == 422


async def test_deleting_requires_one_of_the_two_permissions(
    client: AsyncClient, account: Account
) -> None:
    admin = await headers(client, account.admin_email)
    uploaded = await client.post(
        "/documents", files={"file": ("report.pdf", PDF, "application/pdf")}, headers=admin
    )
    document_id = uploaded.json()["document"]["id"]

    response = await client.delete(
        f"/documents/{document_id}", headers=await headers(client, account.member_email)
    )

    assert response.status_code == 403


async def test_uploading_under_an_unreachable_label_is_refused(
    client: AsyncClient, account: Account
) -> None:
    """The member reaches the default label only, so Finance is not theirs to file under.

    Granting them `documents.upload` isolates the check: this must fail on the label, not
    on the permission.
    """
    async with owner_session() as session:
        await session.execute(
            text(
                "INSERT INTO role_permissions (role_id, permission_code) "
                "SELECT r.id, 'documents.upload' FROM roles r "
                "WHERE r.tenant_id = :t AND r.name = 'member'"
            ),
            {"t": account.tenant_id},
        )

    response = await client.post(
        "/documents",
        files={"file": ("report.pdf", PDF, "application/pdf")},
        data={"labels": [str(account.finance_label)]},
        headers=await headers(client, account.member_email),
    )

    assert response.status_code == 403
