"""Role → context → RLS → rows.

F2 proved that a context carries the labels of the user's roles. F0 proved that policies
filter on those labels. **Neither proves the two compose**, and the composition is the
actual product requirement: an employee sees the documents their function reaches, and
cannot tell the others exist.

Everything here goes through the real HTTP surface and the application database role, so
what is being tested is what a customer runs.
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.common.exceptions import ZenithError
from app.core.database import owner_session, tenant_session
from app.features.auth.router import router as auth_router
from app.features.auth.service import AuthService
from app.features.labels.router import router as labels_router
from app.main import handle_domain_error
from conftest import PASSWORD, Account

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    api = FastAPI()
    api.add_exception_handler(ZenithError, handle_domain_error)  # type: ignore[arg-type]
    api.include_router(auth_router)
    api.include_router(labels_router)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as http:
        yield http


async def _token(client: AsyncClient, email: str) -> str:
    response = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return response.json()["access_token"]


async def _visible_documents(email: str) -> set[UUID]:
    """What this user can actually read, through RLS, as the application role."""
    user_id, tenant_id = AuthService().principal(
        (await AuthService().authenticate(email, PASSWORD)).access_token
    )
    profile = await AuthService().profile(user_id, tenant_id)
    async with tenant_session(profile.context) as session:
        return set(await session.scalars(text("SELECT id FROM documents")))


async def test_a_role_reaching_a_label_sees_the_document(
    account: Account, labelled_document: UUID
) -> None:
    """The whole feature in one assertion.

    `admin` reaches Finance, the document carries Finance, so the document is readable —
    and `member` reaches nothing, so it is not. Nothing in the application filtered
    anything: the query was `SELECT id FROM documents` with no WHERE clause at all.
    """
    assert labelled_document in await _visible_documents(account.admin_email)
    assert labelled_document not in await _visible_documents(account.member_email)


async def test_granting_a_label_to_a_role_grants_the_documents(
    client: AsyncClient, account: Account, labelled_document: UUID
) -> None:
    """The administrative action that is the point of the feature.

    Before: `member` sees nothing. After the label is assigned to their role over HTTP:
    they see it. The user was not touched, their token was not reissued — only the role.
    """
    assert labelled_document not in await _visible_documents(account.member_email)

    async with owner_session() as session:
        member_role = await session.scalar(
            text("SELECT id FROM roles WHERE tenant_id = :t AND name = 'member'"),
            {"t": account.tenant_id},
        )

    response = await client.put(
        f"/roles/{member_role}/labels",
        json={"label_ids": [str(account.finance_label)]},
        headers={"Authorization": f"Bearer {await _token(client, account.admin_email)}"},
    )

    assert response.status_code == 200, response.text
    assert labelled_document in await _visible_documents(account.member_email)


async def test_revoking_the_label_takes_the_documents_back(
    client: AsyncClient, account: Account, labelled_document: UUID
) -> None:
    """Revocation has to work on the next request, not on the next login.

    Labels are resolved per request from the database, so an empty assignment takes effect
    immediately. If this ever regressed to being read from the token, revoking access would
    silently wait fifteen minutes.
    """
    async with owner_session() as session:
        admin_role = await session.scalar(
            text("SELECT id FROM roles WHERE tenant_id = :t AND name = 'admin'"),
            {"t": account.tenant_id},
        )

    await client.put(
        f"/roles/{admin_role}/labels",
        json={"label_ids": []},
        headers={"Authorization": f"Bearer {await _token(client, account.admin_email)}"},
    )

    assert labelled_document not in await _visible_documents(account.admin_email)


async def test_a_user_cannot_infer_the_document_exists(
    account: Account, labelled_document: UUID
) -> None:
    """§2.2's non-inference requirement.

    Not being able to read it is not enough — a count that returns 1 tells the user there
    is something they cannot see, which is exactly the disclosure the rule forbids.
    """
    user_id, tenant_id = AuthService().principal(
        (await AuthService().authenticate(account.member_email, PASSWORD)).access_token
    )
    profile = await AuthService().profile(user_id, tenant_id)

    async with tenant_session(profile.context) as session:
        assert await session.scalar(text("SELECT count(*) FROM documents")) == 0
        assert await session.scalar(text("SELECT count(*) FROM chunks")) == 0
        # Asking for it by id is the other obvious probe.
        found = await session.scalar(
            text("SELECT id FROM documents WHERE id = :id"), {"id": labelled_document}
        )
        assert found is None


async def test_deleting_a_label_in_use_is_refused(
    client: AsyncClient, account: Account, labelled_document: UUID
) -> None:
    """The delete that would widen access.

    An unlabelled document is visible to the whole tenant by design, so removing a
    document's last label through a cascade is a leak with no author. The operation is
    refused while any document carries it.
    """
    response = await client.delete(
        f"/labels/{account.finance_label}",
        headers={"Authorization": f"Bearer {await _token(client, account.admin_email)}"},
    )

    assert response.status_code == 409
    assert "still carry" in response.json()["message"]
    assert labelled_document in await _visible_documents(account.admin_email)


async def test_label_listing_hides_what_the_caller_cannot_reach(
    client: AsyncClient, account: Account
) -> None:
    """A label name is a disclosure by itself — "Project Titan acquisition" says something
    merely by existing. This restriction is application-level, not RLS, which is exactly
    why it needs its own test."""
    async with owner_session() as session:
        await session.execute(
            text("INSERT INTO access_labels (tenant_id, name) VALUES (:t, :n)"),
            {"t": account.tenant_id, "n": f"Secret {uuid4()}"},
        )

    member = await client.get(
        "/labels", headers={"Authorization": f"Bearer {await _token(client, account.member_email)}"}
    )
    admin = await client.get(
        "/labels", headers={"Authorization": f"Bearer {await _token(client, account.admin_email)}"}
    )

    # The member reaches the tenant default and nothing else: not Finance, and not the
    # secret label just created.
    assert [label["id"] for label in member.json()] == [str(account.default_label)]
    # The administrator holds `labels.manage`: managing a set you cannot enumerate is not
    # management. Five now — the default, Finance, HR, the secret one, and the quarantine
    # label, which an administrator has to see precisely because filing what waits there is
    # their job.
    assert len(admin.json()) == 5


async def test_label_management_requires_the_permission(
    client: AsyncClient, account: Account
) -> None:
    response = await client.post(
        "/labels",
        json={"name": f"Nope {uuid4()}"},
        headers={"Authorization": f"Bearer {await _token(client, account.member_email)}"},
    )

    assert response.status_code == 403


async def test_a_label_from_another_tenant_cannot_be_assigned(
    client: AsyncClient, account: Account, labelled_document: UUID
) -> None:
    """RLS would reject the write anyway, but as a constraint violation. The caller gets
    told which id was wrong instead of a 500."""
    async with owner_session() as session:
        foreign_tenant = await session.scalar(
            text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"), {"n": f"Other {uuid4()}"}
        )
        foreign_label = await session.scalar(
            text("INSERT INTO access_labels (tenant_id, name) VALUES (:t, 'Theirs') RETURNING id"),
            {"t": foreign_tenant},
        )

    response = await client.put(
        f"/documents/{labelled_document}/labels",
        json={"label_ids": [str(foreign_label)]},
        headers={"Authorization": f"Bearer {await _token(client, account.admin_email)}"},
    )

    assert response.status_code == 404
    assert "unknown label" in response.json()["message"]
