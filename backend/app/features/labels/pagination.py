"""Keyset pagination for label search, and why it needs its own cursor.

`documents/pagination.py` already solves this problem, and the reasoning there —
`OFFSET` re-evaluates the RLS policy on every discarded row, and shifts under concurrent
writes — applies here unchanged. What does not carry over is the *key*. That cursor
hard-codes `(created_at, id)` because documents have exactly one ordering; labels have
three, and the sort key is a `str`, an `int` or a `datetime` depending on which.

So the sort is encoded *into* the cursor and checked on the way back out. A cursor issued
for `sort=name` means nothing under `sort=usage_count`: the position it names does not
exist in the new ordering, and resuming from it would silently skip or repeat an arbitrary
stretch of the list. Rejecting it is the only answer that does not quietly return wrong
results — the same call `documents/pagination.py` makes for a malformed cursor, for the
same reason.
"""

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, get_args
from uuid import UUID

from app.common.exceptions import InvalidCursorError

DEFAULT_LIMIT = 20
MAX_LIMIT = 100

#: The orderings `GET /labels/search` accepts. `name` ascending is the one a human
#: browsing an alphabetical list expects; the other two are descending, because "most
#: used" and "most recent" are questions about the top of the list, not the bottom.
Sort = Literal["name", "usage_count", "created_at"]
SORTS: tuple[str, ...] = get_args(Sort)

DEFAULT_SORT: Sort = "name"

#: Sort key as it survives a round trip through the cursor. `usage_count` is an aggregate
#: rather than a column, which is exactly why it has to be carried in the cursor: the next
#: page's `WHERE` cannot recompute where the last one stopped without it.
Key = str | int | datetime


@dataclass(frozen=True, slots=True)
class LabelCursor:
    """The last row of the previous page, in the ordering that produced it."""

    sort: Sort
    key: Key
    id: UUID

    def encode(self) -> str:
        key = self.key.isoformat() if isinstance(self.key, datetime) else str(self.key)
        raw = f"{self.sort}|{key}|{self.id}".encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str, expected_sort: Sort) -> "LabelCursor":
        try:
            padded = value + "=" * (-len(value) % 4)
            encoded_sort, _, rest = base64.urlsafe_b64decode(padded).decode().partition("|")
            key, _, identifier = rest.rpartition("|")
            if encoded_sort not in SORTS:
                raise ValueError(f"unknown sort {encoded_sort!r}")
            sort: Sort = encoded_sort  # type: ignore[assignment]  # checked against SORTS above
            if sort != expected_sort:
                # Not an InvalidCursorError by accident: the cursor is perfectly well
                # formed, it just names a position in a different ordering. Silently
                # restarting, or applying it to the new sort anyway, would return a page
                # that looks right and is not.
                raise ValueError(f"cursor is for sort {sort!r}, not {expected_sort!r}")
            return cls(sort=sort, key=_parse(sort, key), id=UUID(identifier))
        except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
            raise InvalidCursorError("this cursor is not one we issued") from exc


def _parse(sort: str, key: str) -> Key:
    if sort == "created_at":
        return datetime.fromisoformat(key)
    if sort == "usage_count":
        return int(key)
    return key


def clamp(limit: int | None) -> int:
    """A caller asking for everything gets a page.

    A lower ceiling than the document listing's 200: this endpoint feeds a search box, and
    a search box that needs a hundred results at once is a search box nobody is reading.
    """
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))
