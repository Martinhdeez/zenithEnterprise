"""Keyset pagination, and why it is not `OFFSET`.

`LIMIT ... OFFSET n` makes Postgres produce and discard `n` rows to reach page ten. Under
RLS that is worse than it sounds: the policy is evaluated on every discarded row too, so
the cost of a deep page is paid in access checks nobody sees the result of. At the 5,000
documents per tenant the configuration permits, the last page costs the most.

It is also wrong rather than merely slow. Uploads arrive while someone is reading, so with
an offset a new document at the top shifts everything down by one and the reader sees the
same row twice — or, on deletion, never sees a row at all. A cursor names a position in the
ordering instead of counting from the start, so concurrent writes cannot move it.

The cursor is opaque on purpose. It encodes `(created_at, id)`, which is an implementation
detail we should be free to change; a client that parsed it would turn our ordering into
part of the public contract.
"""

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.common.exceptions import InvalidCursorError

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass(frozen=True, slots=True)
class Cursor:
    """The last row of the previous page.

    `id` is part of it because `created_at` alone is not unique — two documents uploaded in
    the same transaction share a timestamp, and a cursor on the timestamp alone would skip
    one of them or repeat it forever.
    """

    created_at: datetime
    id: UUID

    def encode(self) -> str:
        raw = f"{self.created_at.isoformat()}|{self.id}".encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str) -> "Cursor":
        try:
            padded = value + "=" * (-len(value) % 4)
            timestamp, _, identifier = base64.urlsafe_b64decode(padded).decode().partition("|")
            return cls(created_at=datetime.fromisoformat(timestamp), id=UUID(identifier))
        except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
            # A 400 rather than an empty page. A malformed cursor means the client is
            # confused about where it is, and silently restarting from the top would hide
            # that behind results that look correct.
            raise InvalidCursorError("this cursor is not one we issued") from exc


def clamp(limit: int | None) -> int:
    """A caller asking for everything gets a page.

    The ceiling is the point: without it, `?limit=1000000` is an unpaginated endpoint with
    extra steps, and the memory problem pagination exists to prevent comes back through the
    query string.
    """
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))
