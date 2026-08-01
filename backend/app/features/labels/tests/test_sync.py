"""The denormalised copy that RLS actually reads.

`documents.label_ids` is a copy of `document_labels`, and it exists because a policy on
`documents` that read `document_labels` — whose own policy reads `documents` — makes
Postgres abort on mutual recursion. The copy is not an optimisation; the direct version
does not run.

Which makes it load-bearing for security. A copy that drifts from its source is a leak or
an outage depending on which way it drifts, so both directions are tested, and so is the
cascade nobody writes by hand.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import owner_session
from app.features.labels.model import AccessLabel, DocumentLabel

# Imported from the central registry rather than from its feature: that module registers
# every model in the shared MetaData, and without it the foreign key from `document_labels`
# to `documents` cannot resolve.
from app.models import Document

pytestmark = pytest.mark.asyncio


async def _document_labels(document_id: UUID) -> set[UUID]:
    async with owner_session() as session:
        row = await session.scalar(
            text("SELECT label_ids FROM documents WHERE id = :id"), {"id": document_id}
        )
        return set(row or [])


async def _chunk_labels(document_id: UUID) -> list[set[UUID]]:
    async with owner_session() as session:
        rows = await session.scalars(
            text("SELECT label_ids FROM chunks WHERE document_id = :id"), {"id": document_id}
        )
        return [set(row or []) for row in rows]


async def test_adding_a_label_updates_the_document_copy(labelled_document: UUID) -> None:
    async with owner_session() as session:
        extra = AccessLabel(
            tenant_id=await _tenant_of(session, labelled_document), name=f"Extra {uuid4()}"
        )
        session.add(extra)
        await session.flush()
        session.add(DocumentLabel(document_id=labelled_document, label_id=extra.id))
        new_label = extra.id

    assert new_label in await _document_labels(labelled_document)


async def test_removing_a_label_updates_the_document_copy(
    labelled_document: UUID, finance_label: UUID
) -> None:
    """Drift in the other direction. A stale label left in the array is a document the
    wrong people keep seeing, long after someone believed they had revoked it."""
    async with owner_session() as session:
        await session.execute(
            text("DELETE FROM document_labels WHERE document_id = :d AND label_id = :l"),
            {"d": labelled_document, "l": finance_label},
        )

    assert finance_label not in await _document_labels(labelled_document)


async def test_deleting_the_label_itself_updates_the_copy(
    labelled_document: UUID, finance_label: UUID
) -> None:
    """The cascade from `access_labels` fires the row trigger on `document_labels`.

    Nobody writes this path by hand, which is exactly why it gets a test: a statement-level
    trigger would not see which documents the cascade touched, and the array would keep an
    id that no longer names anything.
    """
    async with owner_session() as session:
        await session.execute(
            text("DELETE FROM access_labels WHERE id = :id"), {"id": finance_label}
        )

    assert finance_label not in await _document_labels(labelled_document)


async def test_a_chunk_inherits_its_documents_labels_when_created(
    labelled_document: UUID, finance_label: UUID
) -> None:
    """A chunk is created long after its document was labelled — ingestion runs minutes
    later. Without the BEFORE INSERT trigger it is born with an empty array, and an empty
    array means visible to the whole tenant: the passage would be retrievable by everyone
    while the document it came from stayed hidden.

    The fixture inserts the label first and the chunks second, exactly as ingestion will.
    """
    assert all(finance_label in labels for labels in await _chunk_labels(labelled_document))


async def test_the_document_copy_propagates_to_chunks(
    labelled_document: UUID, finance_label: UUID
) -> None:
    """Chunks carry their own copy so the tenant and label filters apply inside the vector
    query without a join penalising the HNSW index. A chunk whose labels disagree with its
    document is a passage retrievable by someone who cannot open the file it came from."""
    assert all(finance_label in labels for labels in await _chunk_labels(labelled_document))

    async with owner_session() as session:
        await session.execute(
            text("DELETE FROM document_labels WHERE document_id = :d"), {"d": labelled_document}
        )

    assert await _chunk_labels(labelled_document) == [set(), set()]


async def test_an_unrelated_document_update_does_not_rewrite_its_chunks(
    labelled_document: UUID,
) -> None:
    """The `IS DISTINCT FROM` guard in the trigger.

    Without it, every status transition during ingestion — pending, parsing, chunking,
    embedding, ready — rewrites every chunk row of the document, five times per upload, for
    a value that did not change. At the 3,000-page ceiling that is a lot of dead work on
    the ingestion path.
    """
    async with owner_session() as session:
        before = await session.scalar(
            text("SELECT max(xmin::text::bigint) FROM chunks WHERE document_id = :d"),
            {"d": labelled_document},
        )
        await session.execute(
            text("UPDATE documents SET status = 'ready' WHERE id = :d"), {"d": labelled_document}
        )

    async with owner_session() as session:
        after = await session.scalar(
            text("SELECT max(xmin::text::bigint) FROM chunks WHERE document_id = :d"),
            {"d": labelled_document},
        )

    # `xmin` is the transaction that last wrote the row. Unchanged means untouched.
    assert before == after


async def _tenant_of(session: AsyncSession, document_id: UUID) -> UUID:
    tenant = await session.scalar(select(Document.tenant_id).where(Document.id == document_id))
    assert tenant is not None
    return tenant
