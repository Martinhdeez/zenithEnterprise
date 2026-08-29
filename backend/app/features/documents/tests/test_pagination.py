"""Keyset pagination, and the two failures an offset would have.

The first is cost: `OFFSET n` makes Postgres produce and discard `n` rows, evaluating the
RLS policy on each of them, so the deepest page is the most expensive one. The second is
correctness, and it is the reason this is worth its own file — a list that shifts while
someone reads it silently repeats or skips rows, and neither leaves a trace.
"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.common.exceptions import InvalidCursorError, NotFoundError
from app.core.database import owner_session
from app.features.documents.pagination import MAX_LIMIT, Cursor, clamp
from app.features.documents.service import DocumentService
from app.features.documents.storage import DocumentStorage
from conftest import Account

from .test_upload import pdf, profile_for


@pytest.fixture
def storage(tmp_path: Path) -> DocumentStorage:
    return DocumentStorage(root=tmp_path / "storage")


async def seed(account: Account, count: int) -> list[UUID]:
    """Documents with distinct timestamps, oldest first.

    Inserted through the owner connection: the point here is the ordering, and driving it
    through `upload` would add a hash and a file write per row without changing what is
    under test.
    """
    created: list[UUID] = []
    async with owner_session() as session:
        default = await session.scalar(
            text("SELECT id FROM access_labels WHERE tenant_id = :t AND is_default"),
            {"t": account.tenant_id},
        )
        for index in range(count):
            document_id = await session.scalar(
                text(
                    "INSERT INTO documents (tenant_id, filename, sha256, size_bytes, created_at) "
                    "VALUES (:t, :name, :sha, 10, :created) RETURNING id"
                ),
                {
                    "t": account.tenant_id,
                    "name": f"doc-{index:02d}.pdf",
                    "sha": f"{index:064x}",
                    "created": datetime(2026, 1, 1, tzinfo=UTC).replace(minute=index),
                },
            )
            await session.execute(
                text("INSERT INTO document_labels (document_id, label_id) VALUES (:d, :l)"),
                {"d": document_id, "l": default},
            )
            created.append(document_id)
    return created


async def test_a_page_stops_at_the_limit_and_hands_back_a_cursor(account: Account) -> None:
    await seed(account, 5)
    service = DocumentService(await profile_for(account))

    first, cursor = await service.page(limit=2)

    assert [document.filename for document in first] == ["doc-04.pdf", "doc-03.pdf"]
    assert cursor is not None


async def test_walking_every_page_visits_each_document_exactly_once(account: Account) -> None:
    """The property that makes pagination usable rather than merely present."""
    await seed(account, 7)
    service = DocumentService(await profile_for(account))

    seen: list[str] = []
    cursor: str | None = None
    while True:
        page, cursor = await service.page(limit=3, cursor=cursor)
        seen.extend(document.filename for document in page)
        if cursor is None:
            break

    assert seen == [f"doc-{index:02d}.pdf" for index in reversed(range(7))]


async def test_the_last_page_returns_no_cursor(account: Account) -> None:
    """The only end-of-list signal there is: there is no total.

    A count under RLS evaluates the policy over every row in the tenant to produce a number
    that is stale by the time the client reads it.
    """
    await seed(account, 3)
    service = DocumentService(await profile_for(account))

    _, cursor = await service.page(limit=3)

    assert cursor is None


async def test_a_document_added_mid_read_does_not_repeat_a_row(
    account: Account, storage: DocumentStorage
) -> None:
    """The correctness failure that offsets have and cursors do not.

    With `OFFSET 2`, an upload landing at the top of the list pushes everything down, and
    the second page begins with a row the reader has already seen. A cursor names a
    position in the ordering, so a new row above it cannot move it.
    """
    await seed(account, 4)
    profile = await profile_for(account)
    service = DocumentService(profile, storage)

    first, cursor = await service.page(limit=2)
    await service.upload("brand-new.pdf", pdf())
    second, _ = await service.page(limit=2, cursor=cursor)

    assert {document.id for document in first} & {document.id for document in second} == set()
    assert "brand-new.pdf" not in [document.filename for document in second]


async def test_the_status_filter_narrows_the_page(account: Account) -> None:
    await seed(account, 3)
    async with owner_session() as session:
        await session.execute(
            text("UPDATE documents SET status = 'ready' WHERE filename = 'doc-01.pdf'")
        )
    service = DocumentService(await profile_for(account))

    ready, _ = await service.page(status="ready")

    assert [document.filename for document in ready] == ["doc-01.pdf"]


async def test_an_unknown_status_is_refused(account: Account) -> None:
    """Rather than silently returning an empty page, which reads as "no documents"."""
    service = DocumentService(await profile_for(account))

    with pytest.raises(NotFoundError):
        await service.page(status="nearly-ready")


async def test_a_tampered_cursor_is_refused(account: Account) -> None:
    """A 400, not an empty page and not the first page.

    A malformed cursor means the client has lost its place. Restarting from the top would
    hide that behind results that look entirely correct.
    """
    service = DocumentService(await profile_for(account))

    for value in ("not-base64!!", "", "AAAA", Cursor(datetime.now(UTC), uuid4()).encode()[:-4]):
        with pytest.raises(InvalidCursorError):
            await service.page(cursor=value or "x")


async def test_a_cursor_survives_the_round_trip() -> None:
    """Including the timestamp, to the microsecond.

    Truncation here would put the cursor between two rows with the same second and either
    repeat them or skip them, depending on which way it rounded.
    """
    original = Cursor(created_at=datetime(2026, 8, 2, 10, 30, 15, 123456, tzinfo=UTC), id=uuid4())

    assert Cursor.decode(original.encode()) == original


def test_the_limit_is_capped() -> None:
    """Without a ceiling, `?limit=1000000` is an unpaginated endpoint with extra steps."""
    assert clamp(None) == 50
    assert clamp(10) == 10
    assert clamp(1_000_000) == MAX_LIMIT
    assert clamp(0) == 1


async def named(account: Account, *filenames: str) -> None:
    """Seed documents with chosen names, labelled so the caller can reach them."""
    async with owner_session() as session:
        default = await session.scalar(
            text("SELECT id FROM access_labels WHERE tenant_id = :t AND is_default"),
            {"t": account.tenant_id},
        )
        for index, filename in enumerate(filenames):
            document_id = await session.scalar(
                text(
                    "INSERT INTO documents (tenant_id, filename, sha256, size_bytes) "
                    "VALUES (:t, :name, :sha, 10) RETURNING id"
                ),
                {"t": account.tenant_id, "name": filename, "sha": f"{index + 900:064x}"},
            )
            await session.execute(
                text("INSERT INTO document_labels (document_id, label_id) VALUES (:d, :l)"),
                {"d": document_id, "l": default},
            )


class TestFindingADocumentByName:
    """The jump-to box, and why the filter is not in the browser.

    The command palette searches documents by name. Filtering one already-fetched page in
    the client would work in a demo and quietly stop working at five hundred documents: the
    box would find what happened to be near the top of the list and miss everything else,
    which reads to the user as the document not existing.
    """

    async def test_matches_part_of_a_filename(self, account: Account) -> None:
        await named(account, "severance-policy.pdf", "travel-expenses.pdf")
        service = DocumentService(await profile_for(account))

        found, _ = await service.page(search="sever")

        assert [document.filename for document in found] == ["severance-policy.pdf"]

    async def test_ignores_case(self, account: Account) -> None:
        await named(account, "Severance-Policy.pdf")
        service = DocumentService(await profile_for(account))

        found, _ = await service.page(search="severance")

        assert [document.filename for document in found] == ["Severance-Policy.pdf"]

    async def test_a_percent_sign_is_matched_literally(self, account: Account) -> None:
        # Otherwise a search for "50%" becomes a wildcard matching everything, which looks
        # like the filter silently not working rather than like a filter doing its job.
        await named(account, "pay-rise-50%.pdf", "travel.pdf")
        service = DocumentService(await profile_for(account))

        found, _ = await service.page(search="50%")

        assert [document.filename for document in found] == ["pay-rise-50%.pdf"]

    async def test_no_search_returns_everything(self, account: Account) -> None:
        await named(account, "a.pdf", "b.pdf")
        service = DocumentService(await profile_for(account))

        found, _ = await service.page()

        assert len(found) == 2
