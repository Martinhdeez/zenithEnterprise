"""The pipeline, against a real Postgres with RLS active.

A stub embedder stands in for TEI: its behaviour is tested in `embeddings/tests`, and a
container plus a model download does not belong in this suite. Everything else here is
real — real policies, real triggers, real cascades.
"""

from collections.abc import Sequence
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.core.database import owner_session, tenant_session
from app.core.hardware import PROFILES
from app.features.documents.service import DocumentService
from app.features.documents.storage import DocumentStorage
from app.features.embeddings.client import DIMENSION
from app.features.ingestion.pipeline import IngestionPipeline
from app.features.tenancy.context import TenantContext
from conftest import Account

from ...documents.tests.test_upload import profile_for


class StubEmbedder:
    """One vector per text, deterministic, right dimension.

    Deterministic so a test can assert which chunk got which vector — the flat list coming
    back from TEI is zipped against the chunks, and a misalignment there attaches every
    embedding to the wrong passage without failing anything.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(item) % 10)] * DIMENSION for item in texts]


@pytest.fixture
def storage(tmp_path: Path) -> DocumentStorage:
    return DocumentStorage(root=tmp_path / "storage")


def pdf_bytes(pages: list[str]) -> bytes:
    """A minimal but genuine PDF, built with pdfplumber's own dependency.

    Not a fixture file: the text has to be known exactly for the assertions, and a checked-in
    binary would drift from what the test claims it contains.
    """
    import io

    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    drawing = canvas.Canvas(buffer, pagesize=letter)
    for body in pages:
        cursor = 720
        for line in body.split("\n"):
            drawing.drawString(72, cursor, line)
            cursor -= 14
        drawing.showPage()
    drawing.save()
    return buffer.getvalue()


LONG_PAGE = "\n".join(
    f"Article {index}. The controller shall implement appropriate measures." for index in range(20)
)


async def upload(
    account: Account, storage: DocumentStorage, content: bytes
) -> tuple[UUID, TenantContext]:
    profile = await profile_for(account)
    result = await DocumentService(profile, storage).upload("report.pdf", _stream(content))
    return result.document.id, TenantContext.for_tenant(account.tenant_id, result.labels)


async def _stream(content: bytes):  # type: ignore[no-untyped-def]
    yield content


async def test_a_document_is_parsed_chunked_embedded_and_marked_ready(
    account: Account, storage: DocumentStorage
) -> None:
    document_id, context = await upload(account, storage, pdf_bytes([LONG_PAGE, LONG_PAGE]))
    embedder = StubEmbedder()

    result = await IngestionPipeline(context, storage, embedder).run(document_id)  # type: ignore[arg-type]

    assert result.status == "ready"
    assert result.pages == 2
    assert result.chunks > 0
    async with tenant_session(context) as session:
        assert await session.scalar(text("SELECT count(*) FROM pages")) == 2
        assert await session.scalar(text("SELECT count(*) FROM chunks")) == result.chunks
        assert await session.scalar(text("SELECT count(*) FROM chunk_embeddings")) == result.chunks
        assert await session.scalar(text("SELECT status FROM documents")) == "ready"


async def test_a_document_with_no_extractable_text_fails_rather_than_succeeding_quietly(
    account: Account, storage: DocumentStorage
) -> None:
    """The rule M0 made non-negotiable.

    Without it this document ingests successfully, appears in the list, gets asked about,
    and is answered from a different document. No exception, no failed status, nothing in
    any log — and no symptom until the customer stops trusting the answers.
    """
    document_id, context = await upload(account, storage, pdf_bytes([" "]))

    result = await IngestionPipeline(context, storage, StubEmbedder()).run(document_id)  # type: ignore[arg-type]

    assert result.status == "failed"
    assert result.chunks == 0
    async with tenant_session(context) as session:
        row = (await session.execute(text("SELECT status, status_detail FROM documents"))).one()
    assert row.status == "failed"
    assert row.status_detail


async def test_low_spec_refuses_a_scan_instead_of_storing_it_empty(
    account: Account, storage: DocumentStorage
) -> None:
    """The profile changes an outcome exactly once, and the change is a refusal.

    OCR is off on `low-spec`, so a page with no text layer cannot be read. Ingesting it as
    "no content" would be the silent failure again, wearing a different hat.
    """
    document_id, context = await upload(account, storage, pdf_bytes([" "]))

    pipeline = IngestionPipeline(context, storage, StubEmbedder(), PROFILES["low-spec"])  # type: ignore[arg-type]
    result = await pipeline.run(document_id)

    assert result.status == "failed"
    assert "OCR is disabled" in (result.detail or "")


async def test_running_twice_does_not_duplicate_anything(
    account: Account, storage: DocumentStorage
) -> None:
    """Procrastinate retries. A document that gained a second copy of every chunk on retry
    would count twice in every later search result."""
    document_id, context = await upload(account, storage, pdf_bytes([LONG_PAGE]))
    pipeline = IngestionPipeline(context, storage, StubEmbedder())  # type: ignore[arg-type]

    first = await pipeline.run(document_id)
    second = await pipeline.run(document_id)

    assert first.chunks == second.chunks
    async with tenant_session(context) as session:
        assert await session.scalar(text("SELECT count(*) FROM chunks")) == second.chunks
        assert await session.scalar(text("SELECT count(*) FROM pages")) == 1


async def test_chunks_inherit_the_documents_labels(
    account: Account, storage: DocumentStorage
) -> None:
    """The hole migration 0003's BEFORE INSERT trigger closed.

    A chunk born with an empty `label_ids` is retrievable by the entire tenant — a passage
    from a Finance document in everyone's results, with the document itself still correctly
    restricted.
    """
    document_id, context = await upload(account, storage, pdf_bytes([LONG_PAGE]))

    await IngestionPipeline(context, storage, StubEmbedder()).run(document_id)  # type: ignore[arg-type]

    async with owner_session() as session:
        empty = await session.scalar(
            text("SELECT count(*) FROM chunks WHERE label_ids = '{}' AND document_id = :d"),
            {"d": document_id},
        )
        document_labels = await session.scalar(
            text("SELECT label_ids FROM documents WHERE id = :d"), {"d": document_id}
        )
        chunk_labels = await session.scalar(
            text("SELECT DISTINCT label_ids FROM chunks WHERE document_id = :d"),
            {"d": document_id},
        )

    assert empty == 0
    assert set(chunk_labels) == set(document_labels)


async def test_a_missing_file_fails_with_a_reason_rather_than_an_exception(
    account: Account, storage: DocumentStorage
) -> None:
    """The upload commits the row before moving the file, so this state is reachable by
    design. It has to read as a document problem, not as a crash."""
    document_id, context = await upload(account, storage, pdf_bytes([LONG_PAGE]))
    async with owner_session() as session:
        sha = await session.scalar(
            text("SELECT sha256 FROM documents WHERE id = :d"), {"d": document_id}
        )
    storage.path_for(account.tenant_id, str(sha)).unlink()

    result = await IngestionPipeline(context, storage, StubEmbedder()).run(document_id)  # type: ignore[arg-type]

    assert result.status == "failed"
    assert "missing" in (result.detail or "")


async def test_a_document_this_context_cannot_see_is_reported_not_crashed(
    account: Account, storage: DocumentStorage
) -> None:
    """The stale-payload case.

    The task payload carries the labels captured at upload. A document relabelled in
    between leaves the worker unable to see it — which is the price of not adding a fifth
    RLS bypass, and it has to fail quietly and legibly rather than throw.
    """
    document_id, _ = await upload(account, storage, pdf_bytes([LONG_PAGE]))
    stale = TenantContext.for_tenant(account.tenant_id, [uuid4()])

    result = await IngestionPipeline(stale, storage, StubEmbedder()).run(document_id)  # type: ignore[arg-type]

    assert result.status == "unknown"


async def test_the_vector_space_is_registered_once(
    account: Account, storage: DocumentStorage
) -> None:
    """`embedding_spaces` is what makes hot reindexing possible: several spaces alive at
    once, the new one built while the active one keeps serving."""
    document_id, context = await upload(account, storage, pdf_bytes([LONG_PAGE]))
    pipeline = IngestionPipeline(context, storage, StubEmbedder())  # type: ignore[arg-type]

    await pipeline.run(document_id)
    await pipeline.run(document_id)

    async with owner_session() as session:
        spaces = await session.scalar(text("SELECT count(*) FROM embedding_spaces"))
    assert spaces == 1


async def test_every_chunk_carries_boxes_and_a_page_number(
    account: Account, storage: DocumentStorage
) -> None:
    """Boxes exist only during parsing. Losing them means a re-parse of the whole corpus
    rather than a re-embed — eight hours per ten thousand pages, by M0's measurement."""
    document_id, context = await upload(account, storage, pdf_bytes([LONG_PAGE]))

    await IngestionPipeline(context, storage, StubEmbedder()).run(document_id)  # type: ignore[arg-type]

    async with tenant_session(context) as session:
        rows = (await session.execute(text("SELECT page_num, bboxes FROM chunks"))).all()

    assert rows
    assert all(row.page_num >= 1 for row in rows)
    assert all(row.bboxes for row in rows)
