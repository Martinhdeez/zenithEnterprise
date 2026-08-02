"""Finding documents that fell out of the pipeline.

Only the finding is tested here, not the enqueuing: `defer_async` needs Procrastinate's
schema and a connection to it, and the interesting logic is which documents are stranded and
which labels they carry now.
"""

from uuid import UUID, uuid4

from sqlalchemy import text

from app.core.database import owner_session
from app.features.ingestion.requeue import find_stranded
from conftest import Account


async def make_document(account: Account, status: str, label_id: UUID | None) -> UUID:
    async with owner_session() as session:
        document_id = await session.scalar(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes, status) "
                "VALUES (:t, :name, :sha, 10, :status) RETURNING id"
            ),
            {
                "t": account.tenant_id,
                "name": f"{status}.pdf",
                "sha": str(uuid4()),
                "status": status,
            },
        )
        if label_id is not None:
            await session.execute(
                text("INSERT INTO document_labels (document_id, label_id) VALUES (:d, :l)"),
                {"d": document_id, "l": label_id},
            )
    return UUID(str(document_id))


async def test_pending_and_failed_are_stranded_but_ready_is_not(account: Account) -> None:
    """`failed` is requeueable on purpose: re-running is exactly what an operator wants
    after fixing the cause — a disk that was full, an embedding service that was down."""
    await make_document(account, "pending", account.default_label)
    await make_document(account, "failed", account.default_label)
    await make_document(account, "ready", account.default_label)

    stranded = await find_stranded(account.tenant_id)

    assert sorted(document.status for document in stranded) == ["failed", "pending"]


async def test_the_labels_are_read_now_not_taken_from_the_old_payload(
    account: Account,
) -> None:
    """The whole reason this command exists.

    A document relabelled after enqueue strands its own job, because the payload carries the
    labels captured at upload — the price of the worker needing no RLS bypass. A requeue
    built on stale labels would strand it a second time.
    """
    document_id = await make_document(account, "pending", account.default_label)
    async with owner_session() as session:
        await session.execute(
            text("UPDATE document_labels SET label_id = :new WHERE document_id = :d"),
            {"new": account.finance_label, "d": document_id},
        )

    stranded = await find_stranded(account.tenant_id)

    assert [document.label_ids for document in stranded] == [[account.finance_label]]


async def test_an_unlabelled_document_is_reported_rather_than_hidden(account: Account) -> None:
    """It cannot be requeued — an empty label set gives the worker a context that reaches
    nothing — but an operator must be able to see that it is there and why it is stuck."""
    await make_document(account, "pending", None)

    stranded = await find_stranded(account.tenant_id)

    assert [document.label_ids for document in stranded] == [[]]


async def test_another_tenants_documents_are_not_included(account: Account) -> None:
    """The command runs on the owner connection, so the tenant filter is application code
    rather than RLS — which makes it exactly the kind of thing that needs its own test."""
    from app.features.tenancy.service import TenantService

    other = await TenantService().create(f"Other {uuid4()}")
    async with owner_session() as session:
        await session.execute(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes, status) "
                "VALUES (:t, 'theirs.pdf', :sha, 10, 'pending')"
            ),
            {"t": other.id, "sha": str(uuid4())},
        )
    await make_document(account, "pending", account.default_label)

    stranded = await find_stranded(account.tenant_id)

    assert [document.filename for document in stranded] == ["pending.pdf"]
