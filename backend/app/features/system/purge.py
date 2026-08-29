"""Destroying everything one organisation ever had.

Runs in the background because it is not quick. Cascading `document_labels` away fires the
per-row trigger `zenith_sync_document_labels` (migration 0003) once for every label on every
document, and on a real corpus that is thousands of trigger invocations — a burst that does
not belong inside an HTTP request holding a connection open.

The order below is not arbitrary; each step exists because doing it later would be wrong.
"""

from uuid import UUID

import structlog
from sqlalchemy import text

from app.core.database import platform_session
from app.features.documents.storage import DocumentStorage
from app.features.tenancy.model import PURGED

log = structlog.get_logger()

#: Every table that owns rows by `tenant_id`, deleted parent-first so the cascades do the
#: rest. Written out rather than discovered from metadata: a table added later should make
#: somebody decide whether a purge must clear it, and a reflective loop would silently
#: decide "yes" — or, worse, silently miss it if the column were named differently.
#:
#: `chunks`, `pages`, `chunk_embeddings`, `document_labels`, `query_citations`, `role_*`,
#: `user_*` and `group_labels` are absent on purpose: every one of them cascades from a
#: parent in this list. Deleting them explicitly would be slower and would hide it if a
#: cascade were ever dropped.
OWNED_TABLES = (
    "documents",
    "queries",
    "groups",
    "access_labels",
    "users",
    "roles",
    "llm_config",
)


async def purge_tenant(tenant_id: UUID) -> dict[str, int]:
    """Remove an organisation's data, leaving a tombstone row behind.

    Returns counts for the log — the only record that will exist afterwards, since the rows
    that would evidence what happened are the ones being deleted.
    """
    cancelled = await _cancel_queued_jobs(tenant_id)

    async with platform_session() as session:
        for table in OWNED_TABLES:
            await session.execute(
                text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id}
            )
        # The row survives. A purged organisation that vanished entirely would free its
        # name for reuse and leave no trace that it ever existed — and "we deleted them,
        # here is when" is the question asked after a purge, not before.
        await session.execute(
            text("UPDATE tenants SET status = :s, status_changed_at = now() WHERE id = :t"),
            {"s": PURGED, "t": tenant_id},
        )

    # After the transaction commits, the same way `DocumentService.delete` orders it: the
    # worst outcome is a file with no row, never a row pointing at a file that is gone.
    files = await DocumentStorage().purge_tenant(tenant_id)

    log.info("tenant_purged", tenant_id=str(tenant_id), files=files, jobs_cancelled=cancelled)
    return {"files": files, "jobs_cancelled": cancelled}


async def _cancel_queued_jobs(tenant_id: UUID) -> int:
    """Cancel this organisation's queued ingestions, before anything is deleted.

    First, and that ordering is the point. `procrastinate_jobs` has no foreign key to
    `tenants` and no RLS — the tenant is a string inside the JSON payload — so nothing
    cascades these away. An `ingest_document` picked up mid-purge would happily write
    chunks for an organisation whose rows had just gone, and the purge would report success
    over a corpus that had started refilling itself.

    Only `todo` jobs are touched. A job already running cannot be recalled; it will fail
    against the deleted rows, which is the correct outcome and is visible in the log.
    """
    async with platform_session() as session:
        # Checked, not assumed. Procrastinate's tables are installed by `zenith
        # install-queue`, not by Alembic, so an installation that has never queued anything
        # does not have them — and a purge that crashed on a missing queue would refuse to
        # delete a customer's data over a table that, by its own absence, proves there is
        # nothing queued to cancel. Asked first rather than caught afterwards: a failed
        # statement aborts the transaction, taking the deletes with it.
        if not await session.scalar(text("SELECT to_regclass('public.procrastinate_jobs')")):
            return 0

        # `RETURNING id` rather than `rowcount`: the count is the thing being reported, and
        # reading it off the driver's cursor is a detail that differs between drivers.
        cancelled = await session.scalars(
            text(
                "UPDATE procrastinate_jobs SET status = 'cancelled' "
                "WHERE status = 'todo' AND args->>'tenant_id' = :t RETURNING id"
            ),
            {"t": str(tenant_id)},
        )
        return len(list(cancelled))
