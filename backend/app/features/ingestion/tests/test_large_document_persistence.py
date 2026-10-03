"""Large replacement writes remain aligned, isolated and atomic under the app role."""

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from app.core.database import tenant_session
from app.features.documents.service import DocumentService
from app.features.documents.storage import DocumentStorage
from app.features.embeddings.client import DIMENSION
from app.features.ingestion.pipeline import IngestionPipeline
from app.features.tenancy.context import TenantContext
from conftest import Account
from conftest import account as seed_account

from ...documents.tests.test_upload import profile_for


@pytest.fixture
async def other_account(configured_engines: None) -> Account:
    return await seed_account.__wrapped__(configured_engines)  # type: ignore[attr-defined]


class IndexedEmbedder:
    def __init__(self, invalid_index: int | None = None) -> None:
        self.invalid_index = invalid_index
        self.texts: list[str] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.texts = list(texts)
        return [
            [float(index + 1)] + [0.0] * (DIMENSION - 1) if index != self.invalid_index else [0.0]
            for index in range(len(texts))
        ]


async def stream(content: bytes) -> AsyncIterator[bytes]:
    yield content


async def uploaded(account: Account, storage: DocumentStorage) -> tuple[UUID, TenantContext]:
    content = "\n\n".join(
        f"Section {index}. " + "The controller preserves the original source evidence. " * 18
        for index in range(220)
    ).encode()
    result = await DocumentService(await profile_for(account), storage).upload(
        "large.txt", stream(content), label_ids=[account.default_label]
    )
    return result.document.id, TenantContext.for_tenant(account.tenant_id, result.labels)


async def snapshot(context: TenantContext, document_id: UUID) -> list[tuple[UUID, str]]:
    async with tenant_session(context) as session:
        rows = await session.execute(
            text("SELECT id, text FROM chunks WHERE document_id = :d ORDER BY char_start"),
            {"d": document_id},
        )
        return [(row.id, row.text) for row in rows]


async def test_large_text_preserves_vector_alignment_labels_and_retry_counts(
    account: Account, other_account: Account, tmp_path: Path
) -> None:
    storage = DocumentStorage(root=tmp_path)
    document_id, context = await uploaded(account, storage)
    embedder = IndexedEmbedder()
    pipeline = IngestionPipeline(context, storage, embedder)  # type: ignore[arg-type]
    first = await pipeline.run(document_id)
    assert first.status == "ready" and first.chunks > 192
    async with tenant_session(context) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT c.text, c.label_ids, (e.embedding::real[])[1] AS first_component "
                    "FROM chunks c JOIN chunk_embeddings e ON e.chunk_id = c.id "
                    "WHERE c.document_id = :d ORDER BY c.char_start"
                ),
                {"d": document_id},
            )
        ).all()
    assert [row.text for row in rows] == embedder.texts
    assert [row.first_component for row in rows] == list(range(1, first.chunks + 1))
    assert all(row.label_ids == list(context.label_ids) for row in rows)
    assert not await snapshot(
        TenantContext.for_tenant(other_account.tenant_id, (other_account.default_label,)),
        document_id,
    )
    assert not await snapshot(TenantContext.for_tenant(account.tenant_id, ()), document_id)
    second = await pipeline.run(document_id)
    assert second.status == "ready" and second.chunks == first.chunks
    assert len(await snapshot(context, document_id)) == first.chunks


async def test_failure_after_multiple_vector_batches_restores_previous_complete_document(
    account: Account, tmp_path: Path
) -> None:
    storage = DocumentStorage(root=tmp_path)
    document_id, context = await uploaded(account, storage)
    valid = IngestionPipeline(context, storage, IndexedEmbedder())  # type: ignore[arg-type]
    assert (await valid.run(document_id)).status == "ready"
    original = await snapshot(context, document_id)
    broken = IngestionPipeline(context, storage, IndexedEmbedder(invalid_index=150))  # type: ignore[arg-type]
    result = await broken.run(document_id)
    assert result.status == "failed"
    assert await snapshot(context, document_id) == original
    async with tenant_session(context) as session:
        assert await session.scalar(text("SELECT count(*) FROM chunk_embeddings")) == len(original)
    recovered = await valid.run(document_id)
    assert recovered.status == "ready" and recovered.chunks == len(original)


async def test_a_new_document_with_a_late_invalid_vector_has_no_partial_index(
    account: Account, tmp_path: Path
) -> None:
    storage = DocumentStorage(root=tmp_path)
    document_id, context = await uploaded(account, storage)
    broken = IngestionPipeline(context, storage, IndexedEmbedder(invalid_index=150))  # type: ignore[arg-type]
    assert (await broken.run(document_id)).status == "failed"
    assert not await snapshot(context, document_id)
    async with tenant_session(context) as session:
        assert await session.scalar(text("SELECT count(*) FROM chunk_embeddings")) == 0
        assert await session.scalar(text("SELECT count(*) FROM pages")) == 0
