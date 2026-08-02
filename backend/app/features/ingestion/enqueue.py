"""Handing a document to the worker.

A separate module from `tasks.py` so the upload path does not import Procrastinate's app —
which opens a connection pool — merely to schedule one job. It also gives tests a single
seam: ingestion is a background concern, and an upload test should not need a worker.

Failing to enqueue is deliberately **not** fatal to the upload. The document is stored, its
labels are correct, and it sits in `pending`; a requeue puts it back in the pipeline. Losing
the customer's file because a queue insert failed would be the worse outcome by a wide
margin.
"""

import os
from uuid import UUID

import structlog

log = structlog.get_logger()

# Set by the test suite. Ingestion needs a worker process and a running TEI, and neither
# belongs in a test about uploading a file.
DISABLED = "ZENITH_DISABLE_INGESTION_QUEUE"


async def enqueue_ingestion(tenant_id: UUID, document_id: UUID, label_ids: list[UUID]) -> None:
    if os.environ.get(DISABLED):
        return

    from app.features.ingestion.tasks import ingest_document

    try:
        await ingest_document.defer_async(
            tenant_id=str(tenant_id),
            document_id=str(document_id),
            label_ids=[str(label) for label in label_ids],
        )
    except Exception:  # noqa: BLE001 - an upload must not be lost to a queue failure
        log.exception("enqueue_failed", document_id=str(document_id))
