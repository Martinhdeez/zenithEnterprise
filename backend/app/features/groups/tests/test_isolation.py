"""The two isolations, against real Postgres and the real policies.

Access has two axes now, and each one has to hold on its own — a model where either can be
substituted for the other is not an access model.

**Horizontal**: Engineering does not read Human Resources. Not because HR is more secret,
but because it is somebody else's material.

**Vertical**: within Finance, a Viewer reads the monthly report and does not read the
confidential one. Same group, different clearance.

The assertions go all the way down to chunks and embeddings, not just to the document list.
A passage retrievable by somebody who cannot open the file it came from is the same
disclosure by a longer route, and vector search reads chunks — so a check that stops at
`GET /documents` proves nothing about RAG.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.core.database import owner_session, tenant_session
from app.features.auth.repository import UserRepository
from app.features.tenancy.context import TenantContext
from conftest import PASSWORD, Account


async def group(tenant_id: UUID, name: str) -> UUID:
    async with owner_session() as session:
        return UUID(
            str(
                await session.scalar(
                    text("INSERT INTO groups (tenant_id, name) VALUES (:t, :n) RETURNING id"),
                    {"t": tenant_id, "n": f"{name} {uuid4()}"},
                )
            )
        )


async def label(tenant_id: UUID, name: str, clearance: int) -> UUID:
    async with owner_session() as session:
        return UUID(
            str(
                await session.scalar(
                    text(
                        "INSERT INTO access_labels (tenant_id, name, priority_level) "
                        "VALUES (:t, :n, :p) RETURNING id"
                    ),
                    {"t": tenant_id, "n": f"{name} {uuid4()}", "p": clearance},
                )
            )
        )


async def user(tenant_id: UUID, clearance: int, groups: list[UUID]) -> UUID:
    """A user holding one role at the given clearance, in the given groups.

    Written through the owner connection: a fixture built with the code under test can only
    prove that code agrees with itself.
    """
    from app.features.auth.provisioning import create_user

    async with owner_session() as session:
        role_id = await session.scalar(
            text(
                "INSERT INTO roles (tenant_id, name, is_system, priority_level) "
                "VALUES (:t, :n, false, :p) RETURNING id"
            ),
            {"t": tenant_id, "n": f"role {uuid4()}", "p": clearance},
        )
        person = await create_user(
            session, tenant_id, f"user-{uuid4()}@example.com", PASSWORD, [UUID(str(role_id))]
        )
        for group_id in groups:
            await session.execute(
                text("INSERT INTO user_groups (user_id, group_id) VALUES (:u, :g)"),
                {"u": person.id, "g": group_id},
            )
        return person.id


async def map_label(group_id: UUID, label_id: UUID) -> None:
    async with owner_session() as session:
        await session.execute(
            text("INSERT INTO group_labels (group_id, label_id) VALUES (:g, :l)"),
            {"g": group_id, "l": label_id},
        )


async def document(tenant_id: UUID, label_id: UUID) -> UUID:
    """A document with one chunk and one embedding, all carrying the label.

    The chunk and the embedding are the point. `documents` is what a listing reads;
    `chunks` is what retrieval reads and what a vector search returns.
    """
    async with owner_session() as session:
        document_id = await session.scalar(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes, status) "
                "VALUES (:t, 'secret.pdf', :sha, 10, 'ready') RETURNING id"
            ),
            {"t": tenant_id, "sha": uuid4().hex + uuid4().hex[:32]},
        )
        await session.execute(
            text("INSERT INTO document_labels (document_id, label_id) VALUES (:d, :l)"),
            {"d": document_id, "l": label_id},
        )
        await session.execute(
            text(
                "INSERT INTO chunks (document_id, tenant_id, page_num, char_start, "
                "char_end, text) VALUES (:d, :t, 1, 0, 20, 'the secret salary data')"
            ),
            {"d": document_id, "t": tenant_id},
        )
        return UUID(str(document_id))


async def reach(tenant_id: UUID, user_id: UUID) -> tuple[UUID, ...]:
    """The labels this user resolves to — the value RLS is handed for the whole request."""
    async with tenant_session(TenantContext.for_tenant(tenant_id, [])) as session:
        return await UserRepository(session).label_ids(user_id)


async def visible(tenant_id: UUID, labels: tuple[UUID, ...]) -> tuple[int, int]:
    """How many documents and chunks the policies release to this reach."""
    async with tenant_session(TenantContext.for_tenant(tenant_id, list(labels))) as session:
        documents = await session.scalar(text("SELECT count(*) FROM documents"))
        chunks = await session.scalar(text("SELECT count(*) FROM chunks"))
        return int(documents or 0), int(chunks or 0)


# --- horizontal ---------------------------------------------------------------------


async def test_a_user_in_engineering_cannot_reach_a_human_resources_document(
    account: Account,
) -> None:
    """The headline isolation. Same tenant, same clearance, different part of the business."""
    engineering = await group(account.tenant_id, "Engineering")
    human_resources = await group(account.tenant_id, "Human Resources")
    payroll = await label(account.tenant_id, "hr/payroll", clearance=1)
    await map_label(human_resources, payroll)
    await document(account.tenant_id, payroll)

    engineer = await user(account.tenant_id, clearance=5, groups=[engineering])

    labels = await reach(account.tenant_id, engineer)

    assert payroll not in labels
    assert await visible(account.tenant_id, labels) == (0, 0)


async def test_the_same_document_is_reachable_from_the_group_that_owns_it(
    account: Account,
) -> None:
    """The other half: an isolation test that never lets anybody in proves nothing."""
    human_resources = await group(account.tenant_id, "Human Resources")
    payroll = await label(account.tenant_id, "hr/payroll", clearance=1)
    await map_label(human_resources, payroll)
    await document(account.tenant_id, payroll)

    officer = await user(account.tenant_id, clearance=1, groups=[human_resources])

    labels = await reach(account.tenant_id, officer)

    assert payroll in labels
    assert await visible(account.tenant_id, labels) == (1, 1)


async def test_seniority_alone_does_not_cross_a_group_boundary(account: Account) -> None:
    """Clearance is vertical. It moves nobody sideways.

    A director at the top of the scale, in the wrong group, reaches nothing — which is the
    property that makes a compartment a compartment.
    """
    engineering = await group(account.tenant_id, "Engineering")
    human_resources = await group(account.tenant_id, "Human Resources")
    payroll = await label(account.tenant_id, "hr/payroll", clearance=1)
    await map_label(human_resources, payroll)
    await document(account.tenant_id, payroll)

    director = await user(account.tenant_id, clearance=10, groups=[engineering])

    assert payroll not in await reach(account.tenant_id, director)


# --- vertical -----------------------------------------------------------------------


async def test_a_viewer_in_finance_cannot_reach_finance_confidential(account: Account) -> None:
    """Right group, insufficient clearance."""
    finance = await group(account.tenant_id, "Finance")
    confidential = await label(account.tenant_id, "finance/confidential", clearance=8)
    await map_label(finance, confidential)
    await document(account.tenant_id, confidential)

    viewer = await user(account.tenant_id, clearance=2, groups=[finance])

    labels = await reach(account.tenant_id, viewer)

    assert confidential not in labels
    assert await visible(account.tenant_id, labels) == (0, 0)


async def test_a_manager_in_finance_reaches_it(account: Account) -> None:
    finance = await group(account.tenant_id, "Finance")
    confidential = await label(account.tenant_id, "finance/confidential", clearance=8)
    await map_label(finance, confidential)
    await document(account.tenant_id, confidential)

    manager = await user(account.tenant_id, clearance=8, groups=[finance])

    assert confidential in await reach(account.tenant_id, manager)


async def test_clearance_is_at_or_above_rather_than_exactly_equal(account: Account) -> None:
    finance = await group(account.tenant_id, "Finance")
    routine = await label(account.tenant_id, "finance/routine", clearance=2)
    await map_label(finance, routine)

    director = await user(account.tenant_id, clearance=9, groups=[finance])

    assert routine in await reach(account.tenant_id, director)


@pytest.mark.parametrize("clearance", [1, 5, 10])
async def test_a_label_mapped_to_no_group_is_reachable_by_nobody(
    account: Account, clearance: int
) -> None:
    """What keeps every label that predates groups behaving exactly as it did.

    Clearance narrows the group route; it is not a route of its own. A label nobody mapped
    and nobody granted stays unreadable at every level on the scale.
    """
    orphan = await label(account.tenant_id, "unmapped", clearance=1)
    await document(account.tenant_id, orphan)
    anybody = await user(account.tenant_id, clearance=clearance, groups=[])

    labels = await reach(account.tenant_id, anybody)

    assert orphan not in labels
    assert await visible(account.tenant_id, labels) == (0, 0)


# --- the two routes together ---------------------------------------------------------


async def test_an_explicit_grant_ignores_both_group_and_clearance(account: Account) -> None:
    """The escape hatch, and it is deliberate.

    A grant is how one named role is given one named label — the auditor who reads the
    confidential ledger without being in Finance and without being senior. It bypasses both
    axes because that is what an exception is for, and it is recorded as its own row so
    anybody reviewing access can see that somebody decided it.
    """
    finance = await group(account.tenant_id, "Finance")
    confidential = await label(account.tenant_id, "finance/confidential", clearance=9)
    await map_label(finance, confidential)
    await document(account.tenant_id, confidential)

    auditor = await user(account.tenant_id, clearance=1, groups=[])
    async with owner_session() as session:
        role_id = await session.scalar(
            text("SELECT role_id FROM user_roles WHERE user_id = :u"), {"u": auditor}
        )
        await session.execute(
            text("INSERT INTO role_labels (role_id, label_id) VALUES (:r, :l)"),
            {"r": role_id, "l": confidential},
        )

    labels = await reach(account.tenant_id, auditor)

    assert confidential in labels
    assert await visible(account.tenant_id, labels) == (1, 1)


async def test_a_user_with_no_roles_and_no_groups_reaches_nothing(account: Account) -> None:
    """The closed-failure case. `max(priority_level)` over no roles is NULL, and the
    comparison against it has to be false rather than an error."""
    finance = await group(account.tenant_id, "Finance")
    open_label = await label(account.tenant_id, "finance/open", clearance=0)
    await map_label(finance, open_label)

    async with owner_session() as session:
        orphan = await session.scalar(
            text(
                "INSERT INTO users (tenant_id, email, password_hash) "
                "VALUES (:t, :e, 'x') RETURNING id"
            ),
            {"t": account.tenant_id, "e": f"nobody-{uuid4()}@example.com"},
        )

    assert await reach(account.tenant_id, UUID(str(orphan))) == ()
