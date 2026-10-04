"""Trusted host upload: only an operator-selected file, never a model-facing tool."""

import argparse
import asyncio
import io
import json
import os
import stat
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx

MAX_BYTES = 64 * 1024 * 1024


class BoundedReader(io.BufferedReader):
    """Refuse growth past the selected file size while multipart streams the file."""

    def __init__(self, raw: io.FileIO, limit: int) -> None:
        super().__init__(raw)
        self.limit = limit

    def read(self, size: int | None = -1, /) -> bytes:
        remaining = max(0, self.limit - self.tell())
        content = super().read(
            min(size, remaining + 1) if size is not None and size >= 0 else remaining + 1
        )
        if len(content) > remaining:
            raise ValueError("the selected file grew while being uploaded")
        return content


def local_api(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not parsed.port
    ):
        raise ValueError("configure an explicit loopback HTTP API origin")
    return value.rstrip("/")


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


@contextmanager
def selected_file(root: Path, relative: Path) -> Generator[BinaryIO]:
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("select a relative file within the approved root")
    if _is_link(root):
        raise ValueError("the selected root must not be a link")
    root = root.resolve(strict=True)
    selected = root / relative
    current = root
    for part in relative.parts:
        current /= part
        if _is_link(current):
            raise ValueError("selected files cannot traverse links or reparse points")
    selected.resolve(strict=True).relative_to(root)
    before = selected.stat()
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_BYTES:
        raise ValueError("select a nonempty regular file of at most 64 MiB")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with io.FileIO(os.open(selected, flags), "rb", closefd=True) as raw:
        after = os.fstat(raw.fileno())
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise ValueError("the selected file changed before opening")
        with BoundedReader(raw, after.st_size) as stream:
            yield cast(BinaryIO, stream)


async def upload_selected(
    client: httpx.AsyncClient, token: str, root: Path, relative: Path, labels: list[UUID]
) -> dict[str, object]:
    if not labels:
        raise ValueError("explicit access labels are required by this host")
    with selected_file(root, relative) as stream:
        response = await client.post(
            "/documents",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (relative.name, stream, "application/octet-stream")},
            data={"labels": [str(label) for label in labels]},
        )
    response.raise_for_status()
    result: dict[str, object] = response.json()
    document = result.get("document")
    if isinstance(document, dict):
        cast(dict[str, object], document).pop("status_detail", None)
    result["http_status"] = response.status_code
    return result


async def wait_ready(
    client: httpx.AsyncClient, token: str, document_id: UUID, seconds: float = 300
) -> dict[str, object]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        response = await client.get(
            f"/documents/{document_id}", headers={"Authorization": f"Bearer {token}"}
        )
        response.raise_for_status()
        result: dict[str, object] = response.json()
        if result.get("status") in {"ready", "failed"}:
            # Do not pass parser paths/provider diagnostics into a model-visible result.
            result.pop("status_detail", None)
            return result
        await asyncio.sleep(0.25)
    raise TimeoutError("the document did not reach a terminal state before the deadline")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True, type=local_api)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--label", required=True, action="append", type=UUID)
    args = parser.parse_args()
    token = os.environ.get("ZENITH_MCP_ACCESS_TOKEN", "").strip()
    if not token:
        raise SystemExit("ZENITH_MCP_ACCESS_TOKEN is required")

    async def run() -> None:
        async with httpx.AsyncClient(base_url=args.api, timeout=60, trust_env=False) as client:
            result = await upload_selected(client, token, args.root, args.file, args.label)
            print(json.dumps(result))

    asyncio.run(run())


if __name__ == "__main__":
    main()
