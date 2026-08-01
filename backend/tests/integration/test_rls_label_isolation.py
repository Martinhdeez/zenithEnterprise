"""MVP gate: label leakage inside a tenant, 0 cases.

Includes the non-inference requirement (mvp.md 2.2): if a user cannot reach a
document, they must not be able to work out that it exists either. Not reading it is
not enough; they must not count it, see it in a total, or discover it via its chunks.
"""

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

pytestmark = pytest.mark.asyncio


@dataclass
class Scenario:
    tenant: UUID
    finance_label: UUID
    hr_label: UUID
    finance_doc: UUID
    hr_doc: UUID
    unlabelled_doc: UUID


@pytest.fixture
async def scenario(owner_engine: AsyncEngine) -> Scenario:
    """One tenant, two labels, three documents: one per label and one general."""
    data = Scenario(
        tenant=uuid4(),
        finance_label=uuid4(),
        hr_label=uuid4(),
        finance_doc=uuid4(),
        hr_doc=uuid4(),
        unlabelled_doc=uuid4(),
    )
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, :n)"),
            {"id": data.tenant, "n": f"Labels {data.tenant}"},
        )
        for label, name in ((data.finance_label, "Finance"), (data.hr_label, "HR")):
            await conn.execute(
                text("INSERT INTO access_labels (id, tenant_id, name) VALUES (:id, :tenant, :n)"),
                {"id": label, "tenant": data.tenant, "n": name},
            )
        documents: tuple[tuple[UUID, str, list[UUID]], ...] = (
            (data.finance_doc, "payroll.pdf", [data.finance_label]),
            (data.hr_doc, "personnel-files.pdf", [data.hr_label]),
            (data.unlabelled_doc, "handbook.pdf", []),
        )
        for doc_id, filename, labels in documents:
            await conn.execute(
                text(
                    "INSERT INTO documents (id, tenant_id, filename, sha256, size_bytes, "
                    "label_ids) VALUES (:id, :tenant, :f, :sha, 10, :labels)"
                ),
                {
                    "id": doc_id,
                    "tenant": data.tenant,
                    "f": filename,
                    "sha": str(uuid4()),
                    "labels": labels,
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO chunks (document_id, tenant_id, label_ids, page_num, "
                    "char_start, char_end, text) "
                    "VALUES (:doc, :tenant, :labels, 1, 0, 10, :body)"
                ),
                {
                    "doc": doc_id,
                    "tenant": data.tenant,
                    "labels": labels,
                    "body": f"content of {filename}",
                },
            )
    return data


async def _session(engine: AsyncEngine, tenant: UUID, labels: list[UUID]) -> AsyncSession:
    session = AsyncSession(engine)
    await session.execute(
        text("SELECT set_config('zenith.tenant_id', :t, true)"), {"t": str(tenant)}
    )
    await session.execute(
        text("SELECT set_config('zenith.label_ids', :l, true)"),
        {"l": ",".join(str(label) for label in labels)},
    )
    return session


async def test_a_role_only_sees_documents_of_its_labels(
    app_engine: AsyncEngine, scenario: Scenario
) -> None:
    session = await _session(app_engine, scenario.tenant, [scenario.finance_label])
    try:
        visible = set(await session.scalars(text("SELECT filename FROM documents")))
    finally:
        await session.close()

    # The unlabelled document is general within the tenant; the HR one does not show.
    assert visible == {"payroll.pdf", "handbook.pdf"}


async def test_existence_cannot_be_inferred_from_a_count(
    app_engine: AsyncEngine, scenario: Scenario
) -> None:
    """A total that included the unreachable document would already be a leak: it
    would reveal that something exists which cannot be seen."""
    session = await _session(app_engine, scenario.tenant, [scenario.finance_label])
    try:
        total = await session.scalar(text("SELECT count(*) FROM documents"))
    finally:
        await session.close()

    assert total == 2


async def test_chunks_inherit_the_restriction(app_engine: AsyncEngine, scenario: Scenario) -> None:
    """Retrieval reads `chunks`, not `documents`. If the label were not enforced
    there, the content would end up quoted inside an answer."""
    session = await _session(app_engine, scenario.tenant, [scenario.finance_label])
    try:
        bodies = set(await session.scalars(text("SELECT text FROM chunks")))
    finally:
        await session.close()

    assert bodies == {"content of payroll.pdf", "content of handbook.pdf"}


async def test_without_labels_only_general_documents_are_visible(
    app_engine: AsyncEngine, scenario: Scenario
) -> None:
    session = await _session(app_engine, scenario.tenant, [])
    try:
        visible = set(await session.scalars(text("SELECT filename FROM documents")))
    finally:
        await session.close()

    assert visible == {"handbook.pdf"}


async def test_pages_inherit_the_restriction(
    app_engine: AsyncEngine, scenario: Scenario, owner_engine: AsyncEngine
) -> None:
    async with owner_engine.begin() as conn:
        for doc in (scenario.finance_doc, scenario.hr_doc):
            await conn.execute(
                text(
                    "INSERT INTO pages (document_id, page_num, extraction_method, text) "
                    "VALUES (:doc, 1, 'pdfplumber', :body)"
                ),
                {"doc": doc, "body": f"page of {doc}"},
            )

    session = await _session(app_engine, scenario.tenant, [scenario.finance_label])
    try:
        pages = list(await session.scalars(text("SELECT document_id FROM pages")))
    finally:
        await session.close()

    assert pages == [scenario.finance_doc]
