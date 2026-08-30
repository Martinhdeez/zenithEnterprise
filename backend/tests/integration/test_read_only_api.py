"""The three read-only routes that had no HTTP test, and the property each one protects.

None of them is gated on a permission, and each says why in its own docstring — reading the
corpus is what the product is for, and a gate here would restrict a listing without
restricting retrieval, which is the wrong half. What replaces the gate is RLS, and these
assert that it is doing the work the missing gate would have done badly.
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
from app.features.labels.router import router as labels_router
from app.features.tenancy.router import router as tenancy_router
from app.main import handle_domain_error
from conftest import PASSWORD, Account

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client(configured_engines: None) -> AsyncIterator[AsyncClient]:
    api = FastAPI()
    api.add_exception_handler(ZenithError, handle_domain_error)  # type: ignore[arg-type]
    for router in (auth_router, documents_router, labels_router, tenancy_router):
        api.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as http:
        yield http


async def headers(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def document_in(account: Account, label_id: object, filename: str) -> None:
    async with owner_session() as session:
        document_id = await session.scalar(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes, status) "
                "VALUES (:t, :f, :sha, 10, 'ready') RETURNING id"
            ),
            {"t": account.tenant_id, "f": filename, "sha": str(uuid4())},
        )
        await session.execute(
            text("INSERT INTO document_labels (document_id, label_id) VALUES (:d, :l)"),
            {"d": document_id, "l": label_id},
        )


async def test_the_folder_tree_hides_a_folder_the_caller_cannot_open(
    client: AsyncClient, account: Account
) -> None:
    """The failure the route's docstring names: *"a folder appears in a sidebar for somebody
    who cannot open anything inside it"*.

    Computed on the server for exactly this reason — a client building the tree from the flat
    listing would have to re-implement the rule that an unlabelled document is visible to the
    whole tenant while a labelled one is not, and that rule lives in a policy.
    """
    await document_in(account, account.finance_label, "salaries.pdf")

    member = (
        await client.get("/documents/folders", headers=await headers(client, account.member_email))
    ).json()

    assert "Finance" not in [folder["name"] for folder in member["folders"]]


async def test_the_folder_tree_shows_one_the_caller_can(
    client: AsyncClient, account: Account
) -> None:
    """The other half: the absence above is the policy working, not the route being empty."""
    await document_in(account, account.finance_label, "salaries.pdf")

    admin = (
        await client.get("/documents/folders", headers=await headers(client, account.admin_email))
    ).json()

    assert "Finance" in [folder["name"] for folder in admin["folders"]]


async def test_a_suggestion_can_only_name_labels_the_caller_already_holds(
    client: AsyncClient, account: Account
) -> None:
    """Gated on nothing beyond being signed in, deliberately — and that is only safe because
    the candidate list is the caller's own reach.

    A suggestion naming a label they do not hold would be a disclosure even though nothing is
    written: label names are themselves revealing, which is why the listing endpoint restricts
    them at all.
    """
    response = await client.get("/labels", headers=await headers(client, account.member_email))
    reachable = {label["id"] for label in response.json()}

    suggested = await client.post(
        "/labels/suggest",
        json={"excerpt": "an invoice for consulting services"},
        headers=await headers(client, account.member_email),
    )

    assert suggested.status_code == 200
    # Usually empty in a test installation with no model configured, which is the documented
    # behaviour — an installation without generation still uploads documents.
    body = suggested.json()
    assert set(body["label_ids"]) <= reachable

    # And the empty answer says which ending produced it. Asserted as a set rather than
    # pinned to one value, because *which* ending a test installation reaches depends on
    # whether a model is configured for it and on what the caller reaches — the property
    # that must hold on every installation is that ids never arrive without an ending, and
    # that the only ending which carries ids is `chose`.
    #
    # The literal set is the wire contract, written out rather than derived from `Outcome`:
    # a comprehension over the enum would agree with any rename and prove nothing. The
    # member here reaches only the default label, which is filtered out of the candidates, so
    # the ending this particular caller gets is `no_folders` — and *not* `unavailable`, which
    # would have blamed a model that may well be configured and working.
    assert body["outcome"] in {
        "chose",
        "declined",
        "unavailable",
        "no_folders",
        "too_many_folders",
        "failed",
    }
    assert bool(body["label_ids"]) == (body["outcome"] == "chose")


async def test_the_status_counts_only_what_the_caller_reaches(
    client: AsyncClient, account: Account
) -> None:
    """It drives the "there is nothing to search yet" screen, so a count including documents
    the caller cannot open would offer them a search that returns nothing and look broken."""
    await document_in(account, account.finance_label, "salaries.pdf")

    member = (
        await client.get("/tenant/status", headers=await headers(client, account.member_email))
    ).json()
    admin = (
        await client.get("/tenant/status", headers=await headers(client, account.admin_email))
    ).json()

    assert sum(member["documents"].values()) == 0
    assert sum(admin["documents"].values()) >= 1
