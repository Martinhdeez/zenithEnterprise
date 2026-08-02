"""Where a customer's bytes actually live.

Content-addressed: the path is derived from the SHA-256 of the contents, never from the
filename the customer sent. That is not a tidiness preference. A file called
`../../etc/passwd.pdf` is a path-traversal attempt, and the whole class of bug disappears
when no attacker-controlled string ever reaches a path. The original name is kept in a
column, shown in the interface, and never resolved against the filesystem.

Files rather than `bytea`, because a 100 MB document in a column travels through the WAL,
the backup and the replication stream. On the 8 GB profile M0 measured, that is the
difference between an installation that restores and one that does not.

The upload is streamed. `await file.read()` on a 100 MB upload from four concurrent users
is a reliable way to reproduce the out-of-memory kill M0 already hit once on the VPS, so
nothing here ever holds a whole document in memory.
"""

import hashlib
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import anyio.to_thread

from app.common.exceptions import LimitExceededError
from app.core.config import settings

# 1 MiB. Large enough that a 100 MB upload is a hundred hops into the thread pool rather
# than a hundred thousand, small enough that peak memory per concurrent upload is a
# rounding error against the profile budget.
CHUNK_BYTES = 1024 * 1024

_HEX_64 = re.compile(r"\A[0-9a-f]{64}\Z")


class CorruptStorageKeyError(Exception):
    """A digest that is not a digest.

    Unreachable through the upload path, which computes the value itself. It exists
    because `path_for` is the single point where a string becomes a filesystem path, and
    the value it is handed will one day come from somewhere else — a repair script, a
    migration, an import. Checking here means the guarantee survives that change.
    """


@dataclass(frozen=True, slots=True)
class Staged:
    """A fully received upload, hashed, not yet in its final place."""

    path: Path
    sha256: str
    size_bytes: int


class DocumentStorage:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or settings.storage_dir)

    @property
    def staging(self) -> Path:
        """Deliberately inside `root`, so the final move is a rename on one filesystem.

        A rename within a filesystem is atomic: the file is either wholly there or wholly
        absent, never a half-written document that ingestion would happily parse. Across
        filesystems the same call becomes copy-then-delete and loses that property, which
        is why staging is not `/tmp`.
        """
        return self.root / "staging"

    def path_for(self, tenant_id: UUID, sha256: str) -> Path:
        if not _HEX_64.match(sha256):
            raise CorruptStorageKeyError(f"not a SHA-256 digest: {sha256!r}")
        # Tenant first, so one customer's documents are one subtree: it makes a per-tenant
        # backup, a per-tenant restore and a tenant deletion each a single path operation.
        return self.root / str(tenant_id) / f"{sha256}.pdf"

    async def stash(self, chunks: AsyncIterator[bytes]) -> Staged:
        """Receive an upload, hashing on the same pass that writes it.

        One pass, so the bytes are read once and never accumulate. The size limit is
        enforced *while* receiving rather than from `Content-Length`, which is a claim the
        client makes and can simply be wrong about.
        """
        await anyio.to_thread.run_sync(lambda: self.staging.mkdir(parents=True, exist_ok=True))
        descriptor, name = tempfile.mkstemp(dir=self.staging, suffix=".part")
        path = Path(name)
        digest = hashlib.sha256()
        size = 0

        try:
            with os.fdopen(descriptor, "wb") as handle:
                # Hash and write together in one hop to the thread pool: both are blocking
                # work on the same buffer, and splitting them would double the context
                # switches for no benefit.
                def write(chunk: bytes) -> None:
                    handle.write(chunk)
                    digest.update(chunk)

                async for chunk in chunks:
                    size += len(chunk)
                    if size > settings.max_file_bytes:
                        raise LimitExceededError(
                            f"file exceeds the {settings.max_file_bytes} byte limit"
                        )
                    await anyio.to_thread.run_sync(write, chunk)
        except BaseException:
            # Includes cancellation. A client that disconnects mid-upload is ordinary, and
            # it must not leave a `.part` file behind on every attempt.
            path.unlink(missing_ok=True)
            raise

        return Staged(path=path, sha256=digest.hexdigest(), size_bytes=size)

    async def commit(self, staged: Staged, tenant_id: UUID) -> Path:
        """Move a staged file into its content-addressed place.

        Idempotent by construction: identical bytes produce an identical path, so a
        document already stored is simply overwritten with the same content. That is what
        makes the deduplication path safe to re-run after a crash.
        """
        destination = self.path_for(tenant_id, staged.sha256)

        def move() -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged.path, destination)

        await anyio.to_thread.run_sync(move)
        return destination

    async def discard(self, staged: Staged) -> None:
        await anyio.to_thread.run_sync(lambda: staged.path.unlink(missing_ok=True))

    async def delete(self, tenant_id: UUID, sha256: str) -> None:
        """Remove the stored file. Missing is success, not an error.

        Deletion runs after the database transaction commits, so it can be reached a
        second time by a retry, and it can be reached for a row whose file never landed.
        Neither is a failure the operator should be told about — the intended state is
        "the file is gone", and it is.
        """
        await anyio.to_thread.run_sync(
            lambda: self.path_for(tenant_id, sha256).unlink(missing_ok=True)
        )

    async def free_bytes(self) -> int:
        """For `zenith diagnose`.

        M0 measured roughly 5 MB of derived data per 100 pages, on top of the original
        file. Running out of disk mid-ingestion is now a foreseeable failure rather than a
        surprise, which makes free space something an operator should be able to read off
        a support report.
        """

        def usage() -> int:
            path = self.root
            while not path.exists() and path != path.parent:
                path = path.parent
            return shutil.disk_usage(path).free

        return await anyio.to_thread.run_sync(usage)
