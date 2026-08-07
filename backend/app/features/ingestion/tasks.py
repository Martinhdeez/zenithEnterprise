"""The ingestion task, and why its payload looks the way it does.

Procrastinate runs on the same Postgres, which is deliberate: an on-premise install already
has one database to back up and monitor, and adding Redis or RabbitMQ to that list is a
cost the customer pays forever for a queue that never exceeds a few thousand jobs.

**The payload carries the tenant and the document's labels.** Writing `chunks` needs an RLS
context whose labels intersect the document's, but reading the document to *learn* its
labels needs those labels already — a worker with none cannot see the document it was told
to ingest. The alternatives were a worker holding every label in the tenant, which is a
compartment-wide widening that something user-facing would eventually reuse, and a fifth
`SECURITY DEFINER` route, which F4's rule forbids for convenience. So the labels travel in
the payload, captured at upload when they were just written.

The cost is a stale payload when a document is relabelled between enqueue and execution.
That fails loudly — the document is simply invisible and the task reports it — and the
relabel is what re-enqueues it. It also makes the task a pure function of its payload,
which is the property that makes a retry safe.
"""

from uuid import UUID

import procrastinate
import structlog

from app.core.config import settings
from app.core.hardware import active as active_profile
from app.features.ingestion.pipeline import IngestionPipeline
from app.features.tenancy.context import TenantContext

# Import-only — see the identical import in `app.main` for why. This module is also the
# worker's own entry point (`procrastinate --app=app.features.ingestion.tasks.app worker`),
# a separate process with its own import graph that never touches `app.main`, so the fix
# there does not cover it: the worker hit the same `NoReferencedTableError` writing a
# `Chunk` row, independently, the first time a real document reached that code path.
from app.models import Base as _Base  # noqa: F401  # pyright: ignore[reportUnusedImport]

log = structlog.get_logger()


def build_app() -> procrastinate.App:
    """Procrastinate on the owner connection.

    The queue tables are ours, not customer data: they hold job ids and payloads, they have
    no RLS, and the worker has to read jobs before it has any tenant context to read them
    with. The tenant context is established *inside* the task, from the payload, which is
    where isolation is actually enforced.
    """
    return procrastinate.App(
        connector=procrastinate.PsycopgConnector(
            conninfo=settings.database_owner_url.replace("postgresql+psycopg://", "postgresql://")
        )
    )


app = build_app()


@app.task(name="ingest_document", queue="ingestion", retry=3)
async def ingest_document(tenant_id: str, document_id: str, label_ids: list[str]) -> dict[str, str]:
    """Parse, chunk and embed one document.

    Strings rather than UUIDs because the payload is JSON in a table; converted once here
    so the rest of the pipeline works in the types it should.

    `retry=3` covers a restarting embedding service, not a bad document. A document that
    cannot be parsed ends in `failed` with a reason — the task itself succeeds, because
    re-running it would produce the same failure and burn the retries for nothing.
    """
    context = TenantContext.for_tenant(UUID(tenant_id), [UUID(label) for label in label_ids])
    result = await IngestionPipeline(context).run(UUID(document_id))

    log.info(
        "ingestion_finished",
        document_id=document_id,
        status=result.status,
        pages=result.pages,
        chunks=result.chunks,
    )
    return {"status": result.status, "chunks": str(result.chunks)}


def worker_concurrency() -> int:
    """One document at a time on `low-spec`, and that is not a suggestion.

    M0 killed TEI twice on this hardware: once by out-of-memory during warm-up with default
    batching, and once with exit 139 when `--max-concurrent-requests` was passed. Serving
    two documents at once means the parser's memory and the embedder's overlap, on a machine
    that has already proven it has no headroom for that.
    """
    return active_profile().ingestion_concurrency
