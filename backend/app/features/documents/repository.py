from uuid import UUID

from sqlalchemy import func, select, tuple_

from app.common.repositories.base import ScopedRepository
from app.features.documents.model import Document
from app.features.documents.pagination import Cursor
from app.features.labels.model import DocumentLabel


class DocumentRepository(ScopedRepository[Document]):
    model = Document

    async def by_sha256(self, sha256: str) -> Document | None:
        """Find an identical file the caller can see.

        "Can see" is doing real work in that sentence. RLS filters `documents` by label,
        so this returns nothing for a document stored under a label the caller does not
        reach — even though the row exists and the unique constraint will say so. The
        service handles that case explicitly; it is not an oversight here.
        """
        return await self.session.scalar(select(Document).where(Document.sha256 == sha256))

    async def page(
        self, limit: int, cursor: Cursor | None = None, status: str | None = None
    ) -> tuple[list[Document], Cursor | None]:
        """One page, newest first, plus the cursor for the next one.

        `limit + 1` rows are fetched and the extra is discarded. That is how the endpoint
        knows whether another page exists without running a `COUNT`, which under RLS would
        evaluate the policy over the whole table to produce a number the caller cannot act
        on anyway.

        The `WHERE` is a row-value comparison rather than
        `created_at < :t OR (created_at = :t AND id < :i)`. Postgres treats the tuple form
        as a single range condition and walks the composite index from that point; the
        expanded form is the same rows and a scan.
        """
        statement = self.query().order_by(Document.created_at.desc(), Document.id.desc())
        if status is not None:
            statement = statement.where(Document.status == status)
        if cursor is not None:
            statement = statement.where(
                tuple_(Document.created_at, Document.id) < (cursor.created_at, cursor.id)
            )

        rows = list(await self.session.scalars(statement.limit(limit + 1)))
        if len(rows) <= limit:
            return rows, None
        page = rows[:limit]
        return page, Cursor(created_at=page[-1].created_at, id=page[-1].id)

    async def count(self) -> int:
        """Documents visible in this context, for the per-tenant limit.

        Visible rather than total, and that is a compromise worth naming: a tenant close to
        the limit could exceed it slightly, because a caller cannot count what their labels
        hide. Counting through the owner would need a bypass, and a bypass added for a
        quota is a poor trade against one added for security.
        """
        return await self.session.scalar(select(func.count()).select_from(Document)) or 0

    async def label_ids_of(self, document_id: UUID) -> set[UUID]:
        """Read from the join table rather than from `documents.label_ids`.

        The array is a denormalised copy maintained by a trigger, and it is what RLS
        reads. The join table is the source. Where they disagree, the source is the one to
        act on — and reading the copy here would let a drift repair itself into the copy
        and quietly become permanent.
        """
        return set(
            await self.session.scalars(
                select(DocumentLabel.label_id).where(DocumentLabel.document_id == document_id)
            )
        )
