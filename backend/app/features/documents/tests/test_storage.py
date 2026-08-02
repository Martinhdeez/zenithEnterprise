"""The storage layer, tested for the properties that would be security bugs.

No database here on purpose: this file is about the filesystem, and the interesting
failures — traversal, a half-written document, a leaked temporary file — are all visible
without one.
"""

from collections.abc import AsyncIterator
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import anyio
import pytest

from app.common.exceptions import LimitExceededError
from app.core.config import settings
from app.features.documents.storage import CorruptStorageKeyError, DocumentStorage


async def stream(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


@pytest.fixture
def storage(tmp_path: Path) -> DocumentStorage:
    return DocumentStorage(root=tmp_path)


async def test_the_digest_is_of_the_whole_content_not_of_one_chunk(
    storage: DocumentStorage,
) -> None:
    """The hash has to survive being fed the file in pieces, because it always will be."""
    staged = await storage.stash(stream(b"%PDF-1.7 ", b"one ", b"document"))

    assert staged.sha256 == sha256(b"%PDF-1.7 one document").hexdigest()
    assert staged.size_bytes == 21


async def test_identical_bytes_land_on_the_same_path(storage: DocumentStorage) -> None:
    """The property deduplication rests on. Different tenants keep different paths."""
    tenant, other = uuid4(), uuid4()

    first = await storage.commit(await storage.stash(stream(b"%PDF-1.7 same")), tenant)
    second = await storage.commit(await storage.stash(stream(b"%PDF-1.7 same")), tenant)
    elsewhere = await storage.commit(await storage.stash(stream(b"%PDF-1.7 same")), other)

    assert first == second
    assert first != elsewhere
    assert first.read_bytes() == b"%PDF-1.7 same"


async def test_the_filename_never_reaches_the_path(storage: DocumentStorage) -> None:
    """Traversal is impossible because no caller-supplied string is used to build a path.

    There is no filename parameter here at all — the path comes from a digest this module
    computed itself. The test states it as a property of the result: whatever is stored
    stays inside the root.
    """
    tenant = uuid4()

    path = await storage.commit(await storage.stash(stream(b"%PDF-1.7 x")), tenant)

    assert storage.root.resolve() in path.resolve().parents


async def test_a_digest_that_is_not_a_digest_is_refused(storage: DocumentStorage) -> None:
    """The guard on the one place a string becomes a path.

    Unreachable today. It exists for the repair script, the importer or the migration that
    will one day hand this function a value it did not compute.
    """
    for key in ("../../etc/passwd", "", "not-hex" * 10, "A" * 64):
        with pytest.raises(CorruptStorageKeyError):
            storage.path_for(uuid4(), key)


async def test_an_oversized_upload_is_stopped_while_it_is_arriving(
    storage: DocumentStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enforced on the bytes received, not on `Content-Length`.

    The header is a claim the client makes. Checking it would leave a lie unpunished, and
    the lie is exactly how the 8 GB profile gets an out-of-memory kill.
    """
    monkeypatch.setattr(settings, "max_file_bytes", 10)

    with pytest.raises(LimitExceededError):
        await storage.stash(stream(b"12345", b"67890", b"and more"))

    assert list(storage.staging.iterdir()) == [], "a rejected upload left a file behind"


async def test_an_interrupted_upload_leaves_nothing_behind(storage: DocumentStorage) -> None:
    """A client disconnecting mid-upload is ordinary traffic.

    If each attempt left a `.part` file, the disk would fill from nothing but flaky
    connections, and the documents already stored would be the casualty.
    """

    async def dies_halfway() -> AsyncIterator[bytes]:
        yield b"%PDF-1.7 "
        raise anyio.EndOfStream

    with pytest.raises(anyio.EndOfStream):
        await storage.stash(dies_halfway())

    assert list(storage.staging.iterdir()) == []


async def test_staging_is_on_the_same_filesystem_as_the_destination(
    storage: DocumentStorage,
) -> None:
    """What makes the final move atomic.

    `os.replace` within one filesystem is all-or-nothing; across filesystems it degrades to
    copy-then-delete, and a crash mid-copy leaves a truncated PDF that ingestion would
    parse without complaint. Asserted on the device number, because the guarantee is a
    property of the filesystem rather than of the path spelling.
    """
    storage.staging.mkdir(parents=True, exist_ok=True)
    (storage.root / "documents-here").mkdir(exist_ok=True)

    assert storage.staging.stat().st_dev == (storage.root / "documents-here").stat().st_dev


async def test_deleting_is_idempotent(storage: DocumentStorage) -> None:
    """Deletion runs after the transaction commits, so a retry reaches it twice.

    It can also be reached for a document whose file never landed. Neither is a failure:
    the intended state is that the file is gone, and it is.
    """
    tenant = uuid4()
    staged = await storage.stash(stream(b"%PDF-1.7 delete me"))
    path = await storage.commit(staged, tenant)

    await storage.delete(tenant, staged.sha256)
    await storage.delete(tenant, staged.sha256)

    assert not path.exists()


async def test_discard_removes_a_staged_file(storage: DocumentStorage) -> None:
    """The path taken when the row cannot be written: the bytes must not survive it."""
    staged = await storage.stash(stream(b"%PDF-1.7 abandoned"))

    await storage.discard(staged)

    assert not staged.path.exists()


async def test_free_space_is_reported_before_the_directory_exists(tmp_path: Path) -> None:
    """`zenith diagnose` runs on installations that have never stored a document.

    Reporting free space has to work then too — that is precisely when an operator is
    checking whether the machine is big enough.
    """
    storage = DocumentStorage(root=tmp_path / "not" / "created" / "yet")

    assert await storage.free_bytes() > 0
