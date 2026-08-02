"""Putting stranded documents back in the pipeline.

Two paths leave a document with no job behind it, both of them chosen deliberately:

- **A failed enqueue does not fail the upload.** The file is stored and its labels are
  correct; losing a customer's document because a queue insert failed would be far worse
  than leaving it in `pending`.
- **A relabelled document strands its own job.** The task payload carries the labels
  captured at upload, so that the worker needs no RLS bypass. Change the labels in between
  and the worker can no longer see the document — which is the price of keeping the bypass
  surface at four routes, and it has to be payable.

Both end the same way, so one command fixes both. It reads through the owner connection
because it is an operator tool run from a terminal, like every other command in the CLI —
not an HTTP path, and not something a tenant can reach.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

from app.core.database import owner_session

# `pending` is a document that never started. `failed` is one that started and stopped, and
# re-running it is exactly what an operator wants after fixing the cause — a missing OCR
# model, an embedding service that was down, a disk that was full.
REQUEUABLE = ("pending", "failed")


@dataclass(frozen=True, slots=True)
class Stranded:
    document_id: UUID
    tenant_id: UUID
    filename: str
    status: str
    label_ids: list[UUID]


async def find_stranded(tenant_id: UUID | None = None, status: str | None = None) -> list[Stranded]:
    """Documents that should be in the pipeline and are not.

    `label_ids` comes from `document_labels` rather than from `documents.label_ids`: the
    array is a denormalised copy maintained by a trigger, and a requeue built from the copy
    would propagate a drift into the worker's context instead of exposing it.
    """
    conditions = ["d.status = ANY(:statuses)"]
    parameters: dict[str, object] = {"statuses": list(REQUEUABLE if not status else [status])}
    if tenant_id is not None:
        conditions.append("d.tenant_id = :tenant_id")
        parameters["tenant_id"] = tenant_id

    async with owner_session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT d.id, d.tenant_id, d.filename, d.status, "
                    "       coalesce(array_agg(dl.label_id) FILTER (WHERE dl.label_id IS NOT NULL),"
                    "                '{}') AS labels "
                    "FROM documents d "
                    "LEFT JOIN document_labels dl ON dl.document_id = d.id "
                    f"WHERE {' AND '.join(conditions)} "
                    "GROUP BY d.id ORDER BY d.created_at"
                ),
                parameters,
            )
        ).all()

    return [
        Stranded(
            document_id=row.id,
            tenant_id=row.tenant_id,
            filename=row.filename,
            status=row.status,
            label_ids=list(row.labels),
        )
        for row in rows
    ]


async def requeue(documents: list[Stranded]) -> int:
    """Enqueue with labels read now, not with whatever the old payload said.

    A document unlabelled at this point is skipped rather than enqueued: an empty label set
    would give the worker a context that reaches nothing, and the job would fail in a way
    that looks like a queue problem rather than a classification one.
    """
    from app.features.ingestion.tasks import ingest_document

    sent = 0
    for document in documents:
        if not document.label_ids:
            continue
        await ingest_document.defer_async(
            tenant_id=str(document.tenant_id),
            document_id=str(document.document_id),
            label_ids=[str(label) for label in document.label_ids],
        )
        sent += 1
    return sent
