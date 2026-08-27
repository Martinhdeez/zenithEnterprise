"""Suspending and destroying an organisation, against real Postgres and real files.

Two properties are worth more than the rest of this file put together:

**Suspension is immediate.** Not "no new tokens" — a session already open stops working on
its very next request. A suspension that waits fifteen minutes for an access token to expire
is not a suspension, it is a scheduling hint.

**A purge takes one organisation and only one.** The neighbour assertions are in the same
tests as the purge assertions on purpose: a `DELETE` with a wrong or missing `WHERE` passes
every test that only looks at the tenant it meant to destroy.
"""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.common.exceptions import ConflictError, TenantSuspendedError
from app.core.config import settings
from app.core.database import owner_session, platform_session
from app.features.auth.service import AuthService
from app.features.system.purge import purge_tenant
from app.features.system.service import SystemService
from app.features.tenancy.service import TenantService
from conftest import PASSWORD, Account


async def document_with_file(tenant_id: UUID, label_id: UUID | None = None) -> str:
    """A document row plus the file on disk it points at.

    Both, because a purge that clears the rows and leaves the bytes is the failure this
    feature exists to prevent — the customer's data is the file, not the row.
    """
    sha = uuid4().hex + uuid4().hex[:32]
    async with owner_session() as session:
        document_id = await session.scalar(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes, status) "
                "VALUES (:t, 'secret.pdf', :sha, 4096, 'ready') RETURNING id"
            ),
            {"t": tenant_id, "sha": sha},
        )
        if label_id:
            await session.execute(
                text("INSERT INTO document_labels (document_id, label_id) VALUES (:d, :l)"),
                {"d": document_id, "l": label_id},
            )
        await session.execute(
            text(
                "INSERT INTO chunks (document_id, tenant_id, page_num, char_start, "
                "char_end, text) VALUES (:d, :t, 1, 0, 10, 'secret text')"
            ),
            {"d": document_id, "t": tenant_id},
        )

    stored = Path(settings.storage_dir) / str(tenant_id) / f"{sha}.pdf"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(b"%PDF-1.7 pretend")
    return sha


async def rows_for(tenant_id: UUID) -> dict[str, int]:
    """How much of this organisation is left, across every table that owns rows by tenant."""
    async with platform_session() as session:
        counts: dict[str, int] = {}
        for table in ("users", "roles", "access_labels", "documents", "chunks", "groups"):
            counts[table] = int(
                await session.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"), {"t": tenant_id}
                )
                or 0
            )
        return counts


# --- suspension -----------------------------------------------------------------------


async def test_a_suspended_organisation_stops_working_on_the_next_request(
    account: Account,
) -> None:
    """The headline behaviour. No waiting for a token to expire."""
    assert await AuthService().profile(account.admin_id, account.tenant_id)

    await SystemService().suspend(account.tenant_id)

    with pytest.raises(TenantSuspendedError):
        await AuthService().profile(account.admin_id, account.tenant_id)


async def test_login_is_refused_while_suspended(account: Account) -> None:
    """Enforced inside `zenith_authenticate_lookup`, so it holds for every caller of it —
    including ones added later that forget there is a status column."""
    from app.common.exceptions import AuthenticationError

    await SystemService().suspend(account.tenant_id)

    with pytest.raises(AuthenticationError):
        await AuthService().authenticate(account.admin_email, PASSWORD)


async def test_reactivating_gives_the_access_back(account: Account) -> None:
    await SystemService().suspend(account.tenant_id)
    await SystemService().activate(account.tenant_id)

    assert await AuthService().profile(account.admin_id, account.tenant_id)
    assert await AuthService().authenticate(account.admin_email, PASSWORD)


async def test_suspending_one_organisation_leaves_the_other_alone(account: Account) -> None:
    other = await TenantService().create(f"Neighbour {uuid4()}")

    await SystemService().suspend(account.tenant_id)

    assert (await SystemService()._one(other.id)).status == "active"  # type: ignore[reportPrivateUsage]


async def test_a_system_administrator_still_reaches_the_panel_while_suspended(
    account: Account,
) -> None:
    """The exception that stops the operator locking themselves out.

    A system administrator's own account lives in some tenant. If suspending that tenant
    also cut them off from `/system`, suspending the wrong organisation would be
    unrecoverable from the product.
    """
    async with owner_session() as session:
        await session.execute(
            text("UPDATE users SET is_system_admin = true WHERE id = :u"),
            {"u": account.admin_id},
        )
    await SystemService().suspend(account.tenant_id)

    profile = await AuthService().profile(account.admin_id, account.tenant_id)

    assert profile.is_system_admin


# --- the brakes on purging ------------------------------------------------------------


async def test_an_active_organisation_cannot_be_purged(account: Account) -> None:
    """Suspend first. The reversible act has to happen, and be seen to work, before the
    irreversible one is available."""
    with pytest.raises(ConflictError, match="Suspend it first"):
        await SystemService().begin_purge(account.tenant_id, "anything")


async def test_the_name_must_match(account: Account) -> None:
    """Checked in the service, not only in the modal: a wrong build of the front end must
    not be able to destroy a customer on its own."""
    await SystemService().suspend(account.tenant_id)
    name = (await SystemService()._one(account.tenant_id)).name  # type: ignore[reportPrivateUsage]

    with pytest.raises(ConflictError, match="name does not match"):
        await SystemService().begin_purge(account.tenant_id, name.upper() + "x")


async def test_the_right_name_opens_it(account: Account) -> None:
    await SystemService().suspend(account.tenant_id)
    name = (await SystemService()._one(account.tenant_id)).name  # type: ignore[reportPrivateUsage]

    marked = await SystemService().begin_purge(account.tenant_id, name)

    assert marked.status == "purging"


# --- the purge itself -----------------------------------------------------------------


async def test_a_purge_removes_every_row_and_every_file(account: Account) -> None:
    sha = await document_with_file(account.tenant_id, account.finance_label)
    stored = Path(settings.storage_dir) / str(account.tenant_id) / f"{sha}.pdf"
    assert stored.exists()

    await purge_tenant(account.tenant_id)

    assert await rows_for(account.tenant_id) == {
        "users": 0,
        "roles": 0,
        "access_labels": 0,
        "documents": 0,
        "chunks": 0,
        "groups": 0,
    }
    assert not stored.exists()
    assert not stored.parent.exists()


async def test_a_purge_leaves_the_neighbour_untouched(account: Account) -> None:
    """Where a `DELETE` with a wrong `WHERE` would show itself — and nowhere else, which is
    why this is not a separate optional test."""
    other = await TenantService().create(f"Neighbour {uuid4()}")
    other_sha = await document_with_file(other.id)
    other_file = Path(settings.storage_dir) / str(other.id) / f"{other_sha}.pdf"

    await document_with_file(account.tenant_id)
    await purge_tenant(account.tenant_id)

    assert (await rows_for(other.id))["documents"] == 1
    assert other_file.exists()


async def test_the_tombstone_survives(account: Account) -> None:
    """The row stays, marked. An organisation that vanished entirely would free its name
    for reuse and leave nothing to answer "what happened to them?" with."""
    await purge_tenant(account.tenant_id)

    organisation = await SystemService()._one(account.tenant_id)  # type: ignore[reportPrivateUsage]

    assert organisation.status == "purged"
    assert organisation.users == 0


async def test_purging_twice_is_not_an_error(account: Account) -> None:
    """`retry=2` on the task only makes sense if the work is idempotent. A retry after a
    partial run has to finish the job, not fail on what the first attempt already did."""
    await purge_tenant(account.tenant_id)
    await purge_tenant(account.tenant_id)

    assert (await rows_for(account.tenant_id))["users"] == 0


async def test_a_purge_reports_what_it_destroyed(account: Account) -> None:
    """The log line is the only record that will exist afterwards — the rows that would
    evidence it are the ones being deleted."""
    await document_with_file(account.tenant_id)
    await document_with_file(account.tenant_id)

    result = await purge_tenant(account.tenant_id)

    assert result["files"] == 2
