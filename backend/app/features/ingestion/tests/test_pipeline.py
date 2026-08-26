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
from app.features.ingestion.pipeline import IngestionPipeline, Result
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
    """A page with no text layer cannot be read, and the refusal is the feature.

    Ingesting it as "no content" would be the silent failure again, wearing a different hat.
    The profile used to decide this and no longer does: `cpu` and `gpu` declared `ocr=True`
    against an engine that does not exist, which made the *same document* end up searchable
    on one profile and absent on another. ADR 0005 allows a profile to change how long
    ingestion takes and never which document a query finds.
    """
    document_id, context = await upload(account, storage, pdf_bytes([" "]))

    pipeline = IngestionPipeline(context, storage, StubEmbedder(), PROFILES["low-spec"])  # type: ignore[arg-type]
    result = await pipeline.run(document_id)

    assert result.status == "failed"
    assert "no OCR is available" in (result.detail or "")


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


async def test_a_mixed_document_says_which_pages_are_not_searchable(
    account: Account, storage: DocumentStorage
) -> None:
    """The silent case, and the one that actually reaches customers.

    A fully scanned PDF fails loudly and always did. A *mixed* one — a contract with two
    scanned signature pages, a report with a photographed appendix — behaved far worse: the
    readable pages produced chunks, the document reached `ready`, and the scanned pages were
    simply not in the index. Nothing said so. A search over that document answers
    confidently from the part it happens to have, which is the same failure shape as a silent
    leak and gets the same treatment here.

    The page numbers are asserted, not just the count. Somebody holding the PDF needs to know
    where to look, and "3 pages" does not tell them.
    """
    document_id, context = await upload(
        account, storage, pdf_bytes([LONG_PAGE, " ", LONG_PAGE, " "])
    )

    pipeline = IngestionPipeline(context, storage, StubEmbedder(), PROFILES["cpu"])  # type: ignore[arg-type]
    result = await pipeline.run(document_id)

    # Ready, correctly: two of the four pages are searchable, and refusing the whole document
    # would throw away work somebody can use.
    assert result.status == "ready"

    async with tenant_session(context) as session:
        detail = await session.scalar(
            text("SELECT status_detail FROM documents WHERE id = :d"), {"d": document_id}
        )

    assert detail is not None
    assert "2 of 4 page(s) are not searchable" in detail
    assert "page 2, 4" in detail
    assert "OCR" in detail


async def test_a_profile_that_permits_ocr_does_not_change_what_is_indexed(
    account: Account, storage: DocumentStorage
) -> None:
    """ADR 0005's rule, tested where it was being broken.

    A profile may change how long ingestion takes; it may never change which document a query
    finds. `cpu` and `gpu` declared `ocr=True` against an engine nothing has wired, so the
    same scanned page was dropped silently there and refused on `low-spec` — the same PDF,
    two different corpora, decided by a hardware setting.
    """
    outcomes: list[Result] = []
    seen: set[UUID] = set()
    for index, name in enumerate(("low-spec", "cpu", "gpu")):
        # A distinguishable document per profile. Identical bytes deduplicate into one
        # document by sha256, and three profiles re-ingesting the same row would compare a
        # result against itself.
        document_id, context = await upload(
            account, storage, pdf_bytes([f"{LONG_PAGE} profile {index}", " "])
        )
        seen.add(document_id)
        pipeline = IngestionPipeline(context, storage, StubEmbedder(), PROFILES[name])  # type: ignore[arg-type]
        outcomes.append(await pipeline.run(document_id))

    assert len(seen) == 3, "the three runs must be three documents, not one deduplicated row"
    assert {outcome.status for outcome in outcomes} == {"ready"}
    assert {outcome.pages for outcome in outcomes} == {2}
    # The number that matters: the scanned page is out of the index on every profile, so the
    # searchable content of the same PDF does not depend on the hardware it landed on.
    assert len({outcome.chunks for outcome in outcomes}) == 1


async def test_a_document_past_the_page_limit_is_refused_before_it_is_parsed(
    account: Account, storage: DocumentStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`settings.max_pages_per_document` existed and nothing read it.

    A safeguard in name only, of the same kind as an abort listener nothing could reach. The
    refusal comes *before* parsing, which is the only place it saves anything: afterwards the
    memory and the time have already been spent, and the whole point of a limit is not to
    spend them.

    The message names both numbers. Somebody holding a 4,000-page manual can act on "more than
    the 3-page limit"; they cannot act on "too large".
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "max_pages_per_document", 3)
    document_id, context = await upload(
        account, storage, pdf_bytes([LONG_PAGE, LONG_PAGE, LONG_PAGE, LONG_PAGE])
    )

    embedder = StubEmbedder()
    result = await IngestionPipeline(context, storage, embedder, PROFILES["cpu"]).run(document_id)  # type: ignore[arg-type]

    assert result.status == "failed"
    assert "4 pages" in (result.detail or "")
    assert "3-page limit" in (result.detail or "")
    # Nothing was embedded, which is the cost the limit exists to avoid.
    assert embedder.calls == []


async def test_a_document_within_the_limit_is_unaffected(
    account: Account, storage: DocumentStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boundary is `>`, not `>=`: a document of exactly the limit is allowed."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "max_pages_per_document", 2)
    document_id, context = await upload(account, storage, pdf_bytes([LONG_PAGE, LONG_PAGE]))

    result = await IngestionPipeline(context, storage, StubEmbedder(), PROFILES["cpu"]).run(  # type: ignore[arg-type]
        document_id
    )

    assert result.status == "ready"


async def test_the_document_is_not_ready_while_it_is_being_filed(
    account: Account, storage: DocumentStorage
) -> None:
    """Migration 0017's uploader exception is `status <> 'ready'`.

    `_persist` used to write `ready` in the same transaction as the chunks, which switched the
    exception off during the one step it was written for — the model call. A member who
    uploaded the file lost it from Documents while a classifier was being asked where it
    belongs, and the upload screen's poller began reporting "not found".

    Asserted from *inside* the filing step rather than by setting the status by hand: the
    property is that the pipeline reaches this point without having written `ready`, and only
    a classifier that looks while it is being called can say so.
    """
    from app.features.ingestion.classification import Filing, Outcome
    from app.features.ingestion.pipeline import IngestionPipeline

    seen: list[str] = []

    class LooksAtTheStatus:
        async def file(self, document_id: UUID, *_args: object, **_kwargs: object) -> Filing:
            async with owner_session() as session:
                status = await session.scalar(
                    text("SELECT status FROM documents WHERE id = :d"), {"d": document_id}
                )
            seen.append(str(status))
            return Filing([], Outcome.DECLINED)

    document_id, context = await upload(account, storage, pdf_bytes([LONG_PAGE]))
    pipeline = IngestionPipeline(
        context,
        storage,
        StubEmbedder(),  # type: ignore[arg-type]
        PROFILES["cpu"],
        classifier=LooksAtTheStatus(),  # type: ignore[arg-type]
    )

    result = await pipeline.run(document_id)

    assert seen == ["classifying"], "the exception is keyed on `status <> 'ready'`"
    # And it does end up ready — the chunks are committed and the document is searchable.
    assert result.status == "ready"
    async with owner_session() as session:
        final = await session.scalar(
            text("SELECT status FROM documents WHERE id = :d"), {"d": document_id}
        )
    assert final == "ready"


# --- documents that were never PDFs -------------------------------------------------------

MARKDOWN_NOTE = (
    "# Collector runbook\n\n"
    "Restart the collector before the reconciler, never the other way round. "
    "A reconciler started first reads a partial window and writes a gap it will not revisit.\n\n"
    "## Escalation\n\n"
    "Page the on-call engineer if the backlog exceeds four hours.\n"
) * 6


async def upload_text(
    account: Account, storage: DocumentStorage, filename: str, body: str
) -> tuple[UUID, TenantContext]:
    profile = await profile_for(account)
    result = await DocumentService(profile, storage).upload(filename, _stream(body.encode()))
    return result.document.id, TenantContext.for_tenant(account.tenant_id, result.labels)


async def test_a_markdown_document_is_ingested_and_searchable(
    account: Account, storage: DocumentStorage
) -> None:
    """The whole path, with no PDF anywhere in it."""
    document_id, context = await upload_text(account, storage, "runbook.md", MARKDOWN_NOTE)

    result = await IngestionPipeline(context, storage, StubEmbedder()).run(document_id)  # type: ignore[arg-type]

    assert result.status == "ready"
    assert result.chunks > 0
    async with tenant_session(context) as session:
        # One stored unit: the file. Not one per screenful — a pretend page would put a
        # page number on a citation that has none.
        assert await session.scalar(text("SELECT count(*) FROM pages")) == 1
        assert await session.scalar(text("SELECT count(*) FROM chunk_embeddings")) == result.chunks


async def test_a_text_documents_chunks_carry_no_page_number(
    account: Account, storage: DocumentStorage
) -> None:
    """Null all the way to the column, so no reader can render "page 1".

    Asserted against the database rather than the dataclass: the model made the column
    nullable and a default anywhere between here and there would quietly fill it.
    """
    document_id, context = await upload_text(account, storage, "notes.txt", MARKDOWN_NOTE)

    await IngestionPipeline(context, storage, StubEmbedder()).run(document_id)  # type: ignore[arg-type]

    async with tenant_session(context) as session:
        numbered = "SELECT count(*) FROM chunks WHERE page_num IS NOT NULL"
        assert await session.scalar(text("SELECT count(*) FROM chunks")) > 0
        assert await session.scalar(text(numbered)) == 0


async def test_a_text_documents_offsets_select_its_own_text(
    account: Account, storage: DocumentStorage
) -> None:
    """End to end, against what was actually stored.

    The chunker's own test proves the arithmetic. This proves the arithmetic survived
    parsing, normalisation and the round trip through Postgres — which is where an offset
    computed before `\\r\\n` collapsed would come apart.
    """
    document_id, context = await upload_text(account, storage, "runbook.md", MARKDOWN_NOTE)

    await IngestionPipeline(context, storage, StubEmbedder()).run(document_id)  # type: ignore[arg-type]

    async with tenant_session(context) as session:
        stored = await session.scalar(text("SELECT text FROM pages"))
        rows = (await session.execute(text("SELECT char_start, char_end, text FROM chunks"))).all()

    assert rows
    for row in rows:
        assert stored[row.char_start : row.char_end].strip() == row.text


async def test_windows_line_endings_do_not_shift_the_highlight(
    account: Account, storage: DocumentStorage
) -> None:
    """A file written on Windows must chunk and highlight identically to the same file
    written anywhere else.

    Normalising after the offsets were computed would move every highlight by one character
    per preceding line — a drift that grows down the document and looks like an off-by-one
    nobody can reproduce on their own machine.
    """
    document_id, context = await upload_text(
        account, storage, "windows.md", MARKDOWN_NOTE.replace("\n", "\r\n")
    )

    await IngestionPipeline(context, storage, StubEmbedder()).run(document_id)  # type: ignore[arg-type]

    async with tenant_session(context) as session:
        stored = await session.scalar(text("SELECT text FROM pages"))
        rows = (await session.execute(text("SELECT char_start, char_end, text FROM chunks"))).all()

    assert "\r" not in stored
    for row in rows:
        assert stored[row.char_start : row.char_end].strip() == row.text
