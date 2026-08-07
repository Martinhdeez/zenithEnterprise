"""Label search: what it finds, what it counts, and who it refuses to tell.

Two properties carry the weight here and neither is about matching strings.

A label name is a disclosure — "Project Titan acquisition" says something merely by
existing — so search has to enforce the same reachability rule `GET /labels` does, or it
becomes the enumeration endpoint that list deliberately is not.

And the usage count is RLS-scoped by construction: `document_labels` inherits its policy
from `documents`, so the number is "documents you can see carrying this label", never the
size of the compartment. That is a property of the policy rather than of this code, which
is exactly why it is asserted from the outside.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.common.exceptions import InvalidCursorError
from app.core.database import owner_session
from app.features.labels.model import AccessLabel, RoleLabel
from app.features.labels.service import LabelService
from app.features.tenancy.context import TenantContext
from conftest import Account

pytestmark = pytest.mark.asyncio


async def label(tenant_id: UUID, name: str, reachable_by: UUID | None = None) -> UUID:
    """A label, written through the owner connection so the fixture cannot agree with the
    code under test by construction."""
    async with owner_session() as session:
        created = AccessLabel(tenant_id=tenant_id, name=name)
        session.add(created)
        await session.flush()
        if reachable_by:
            session.add(RoleLabel(role_id=reachable_by, label_id=created.id))
        return created.id


async def document(tenant_id: UUID, label_ids: list[UUID], name: str = "doc.pdf") -> UUID:
    async with owner_session() as session:
        document_id = await session.scalar(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes) "
                "VALUES (:t, :name, :sha, 10) RETURNING id"
            ),
            {"t": tenant_id, "name": name, "sha": uuid4().hex + uuid4().hex[:32]},
        )
        for label_id in label_ids:
            await session.execute(
                text("INSERT INTO document_labels (document_id, label_id) VALUES (:d, :l)"),
                {"d": document_id, "l": label_id},
            )
    return UUID(str(document_id))


def names(result: object) -> list[str]:
    return [label.name for label, _ in result.labels]  # type: ignore[attr-defined]


async def test_a_manager_searches_every_label_in_the_tenant(account: Account) -> None:
    """`labels.manage` searches what it can manage. Managing a set you cannot enumerate is
    not management — the same call `visible()` already makes."""
    await label(account.tenant_id, "Payroll archive")

    service = LabelService(TenantContext.for_tenant(account.tenant_id, [account.default_label]))
    result = await service.search(may_manage=True, query="payroll")

    assert names(result) == ["Payroll archive"]


async def test_a_member_never_finds_a_label_they_cannot_reach(account: Account) -> None:
    """The assertion this endpoint lives or dies on.

    A member searching "payroll" and getting a hit learns that a Payroll label exists,
    which is the inference mvp.md 3.1 forbids — the name is the leak, before any document
    behind it is touched.
    """
    await label(account.tenant_id, "Payroll archive")

    service = LabelService(TenantContext.for_tenant(account.tenant_id, [account.default_label]))
    result = await service.search(may_manage=False, query="payroll")

    assert names(result) == []


async def test_search_matches_a_substring_case_insensitively(account: Account) -> None:
    await label(account.tenant_id, "Quarterly Reports")

    service = LabelService(TenantContext.for_tenant(account.tenant_id, []))
    result = await service.search(may_manage=True, query="TERLY REP")

    assert names(result) == ["Quarterly Reports"]


async def test_a_wildcard_typed_into_the_search_box_is_a_literal(account: Account) -> None:
    """`%` and `_` are `LIKE` metacharacters, and a user typing them means the characters.

    Unescaped, a search for `%` matches every label in the tenant — which for a member is
    the enumeration the reachability rule exists to prevent, reachable by typing one
    character.
    """
    await label(account.tenant_id, "100% owned")
    await label(account.tenant_id, "Subsidiaries")

    service = LabelService(TenantContext.for_tenant(account.tenant_id, []))
    result = await service.search(may_manage=True, query="%")

    assert names(result) == ["100% owned"]


async def test_the_count_is_what_the_caller_can_see_not_what_exists(account: Account) -> None:
    """The RLS-scoped count, asserted from outside the code that produces it.

    Two documents carry Finance; the caller reaches only the default label. The policy on
    `documents` hides both, `document_labels` inherits that, and the count has to come
    back zero — a non-zero number here would be telling the caller how big a compartment
    they were not admitted to is.
    """
    await document(account.tenant_id, [account.finance_label], name="one.pdf")
    await document(account.tenant_id, [account.finance_label], name="two.pdf")

    service = LabelService(TenantContext.for_tenant(account.tenant_id, [account.default_label]))
    result = await service.search(may_manage=True, query="Finance")

    assert [count for _, count in result.labels] == [0]

    reaching = LabelService(TenantContext.for_tenant(account.tenant_id, [account.finance_label]))
    seen = await reaching.search(may_manage=True, query="Finance")
    assert [count for _, count in seen.labels] == [2]


async def test_pages_walk_the_whole_list_without_repeating_or_skipping(account: Account) -> None:
    """The property keyset pagination exists to have.

    Asserted as a walk rather than page by page: a cursor that is off by one shows up as a
    duplicate or a gap in the concatenation, and neither is visible from a single page.
    """
    for index in range(7):
        await label(account.tenant_id, f"Walk {index:02d}")

    service = LabelService(TenantContext.for_tenant(account.tenant_id, []))
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):  # generous ceiling; the loop breaks on the last page
        page = await service.search(may_manage=True, query="Walk", cursor=cursor, limit=2)
        seen.extend(names(page))
        cursor = page.next_cursor
        if cursor is None:
            break

    assert cursor is None, "pagination did not terminate"
    assert seen == sorted(seen), "the ordering was not stable across pages"
    assert seen == [f"Walk {index:02d}" for index in range(7)]


async def test_the_last_page_reports_no_cursor(account: Account) -> None:
    """A full page is not the same as a next page. Without the extra row the repository
    fetches, a list whose length is an exact multiple of the limit would hand out a cursor
    to an empty page."""
    for index in range(4):
        await label(account.tenant_id, f"Exact {index}")

    service = LabelService(TenantContext.for_tenant(account.tenant_id, []))
    page = await service.search(may_manage=True, query="Exact", limit=4)

    assert len(page.labels) == 4
    assert page.next_cursor is None


async def test_sorting_by_usage_puts_the_most_carried_label_first(account: Account) -> None:
    busy = await label(account.tenant_id, "Busy")
    quiet = await label(account.tenant_id, "Quiet")
    for index in range(3):
        await document(account.tenant_id, [busy], name=f"busy-{index}.pdf")
    await document(account.tenant_id, [quiet], name="quiet.pdf")

    service = LabelService(TenantContext.for_tenant(account.tenant_id, [busy, quiet]))
    result = await service.search(may_manage=True, sort="usage_count", limit=2)

    assert [(label.name, count) for label, count in result.labels] == [("Busy", 3), ("Quiet", 1)]


async def test_usage_pages_stay_stable_when_counts_tie(account: Account) -> None:
    """Every label here has zero documents, so the count alone cannot order them.

    This is the case the `id` tiebreak in the cursor exists for: without it Postgres may
    return tied rows in a different order per query, and a cursor into an unstable
    ordering repeats rows or drops them.
    """
    for index in range(5):
        await label(account.tenant_id, f"Tied {index}")

    service = LabelService(TenantContext.for_tenant(account.tenant_id, []))
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        page = await service.search(
            may_manage=True, query="Tied", sort="usage_count", cursor=cursor, limit=2
        )
        seen.extend(names(page))
        cursor = page.next_cursor
        if cursor is None:
            break

    assert sorted(seen) == [f"Tied {index}" for index in range(5)]
    assert len(seen) == len(set(seen)), "a tied row was returned twice"


async def test_a_cursor_from_a_different_sort_is_rejected(account: Account) -> None:
    """It names a position in an ordering that is no longer in effect.

    Applying it anyway returns a page that looks plausible and is wrong, which is worse
    than an error: the client has no way to notice.
    """
    for index in range(3):
        await label(account.tenant_id, f"Switch {index}")

    service = LabelService(TenantContext.for_tenant(account.tenant_id, []))
    page = await service.search(may_manage=True, query="Switch", limit=1)
    assert page.next_cursor is not None

    with pytest.raises(InvalidCursorError):
        await service.search(
            may_manage=True, query="Switch", sort="usage_count", cursor=page.next_cursor
        )


async def test_a_forged_cursor_is_rejected(account: Account) -> None:
    service = LabelService(TenantContext.for_tenant(account.tenant_id, []))

    with pytest.raises(InvalidCursorError):
        await service.search(may_manage=True, cursor="not-a-cursor-we-issued")
