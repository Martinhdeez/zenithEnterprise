from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse

from app.common.exceptions import LimitExceededError, NotFoundError
from app.core.config import settings
from app.features.auth.dependencies import CurrentProfile, requires, requires_any
from app.features.documents.folders import tree
from app.features.documents.pagination import MAX_LIMIT
from app.features.documents.schemas import (
    DocumentPage,
    DocumentResponse,
    FolderResponse,
    FolderTreeResponse,
    UploadResponse,
)
from app.features.documents.service import DELETE_ANY, DELETE_OWN, UPLOAD, DocumentService
from app.features.documents.storage import CHUNK_BYTES, DocumentStorage

router = APIRouter(tags=["documents"])


@router.post(
    "/documents",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(requires(UPLOAD))],
)
async def upload_document(
    request: Request,
    response: Response,
    profile: CurrentProfile,
    file: Annotated[UploadFile, File()],
    labels: Annotated[list[UUID] | None, Form()] = None,
    # Both optional, both display-only overrides of what the file itself would give:
    # `filename` defaults to the name the browser sent, `description` has no fallback at
    # all. Neither reaches ingestion or search, so there's nothing here to validate beyond
    # "is it a string" — Pydantic/FastAPI already do that.
    filename: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
) -> UploadResponse:
    """Store a document and classify it.

    `201` for a document that was stored, `200` when identical bytes were already held and
    this upload only contributed its labels. The distinction is not cosmetic: a client that
    treats both as "created" would report a new document to the user where none exists.
    """
    _reject_obviously_oversized(request)

    result = await DocumentService(profile).upload(
        filename=filename or file.filename or "document.pdf",
        chunks=_stream(file),
        label_ids=labels,
        description=description,
    )
    if result.deduplicated:
        response.status_code = status.HTTP_200_OK
    return UploadResponse(
        document=DocumentResponse.model_validate(result.document),
        labels=result.labels,
        deduplicated=result.deduplicated,
    )


@router.get(
    "/documents/folders",
    operation_id="listFolders",
    summary="The label structure, aggregated into a browsable tree",
    responses={401: {"description": "Missing or invalid credentials"}},
)
async def folders(profile: CurrentProfile) -> FolderTreeResponse:
    """Grouping computed here rather than in the browser.

    A client building this from the flat listing would need every document to do it, would
    disagree with the list about counts the moment either changed, and — the reason that
    settles it — would have to re-implement the rule that a document with no labels is
    visible to the whole tenant while a labelled one is not. That rule lives in the RLS
    policy, and reconstructing it client-side is how a folder appears in a sidebar for
    somebody who cannot open anything inside it.

    Gated on authentication alone: every role can already discover what it can reach by
    searching, so a permission here would protect nothing and break the first screen for
    the lowest-privileged user.
    """
    computed = await tree(profile.context)
    return FolderTreeResponse(
        folders=[FolderResponse(**asdict(folder)) for folder in computed.folders],
        total_documents=computed.total_documents,
    )


@router.get("/documents")
async def list_documents(
    profile: CurrentProfile,
    limit: Annotated[int | None, Query(ge=1, le=MAX_LIMIT)] = None,
    cursor: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    label_id: UUID | None = None,
    # `unlabelled=true` rather than `label_id=null`: query strings have no way to write
    # "the parameter is present and its value is null" distinctly from "the parameter is
    # absent", so the one folder with no id needs its own flag to be requestable at all.
    unlabelled: bool = False,
    search: Annotated[str | None, Query(description="Matched against the filename.")] = None,
) -> DocumentPage:
    """The documents the caller's labels reach, newest first, one page at a time.

    No permission gate: reading the corpus is what the product is for, and RLS already
    decides which rows exist for this caller. A gate here would restrict the list without
    restricting retrieval, which is the wrong half.

    Paginated with a cursor rather than an offset. A tenant may hold five thousand
    documents, and uploads arrive while someone is reading — an offset would show a reader
    the same row twice each time a document was added above them.

    `label_id` narrows to one folder from `GET /documents/folders`. Requesting a label the
    caller cannot reach is not an error — RLS already hides those rows from the base
    query, so the filter just narrows an empty set to a smaller empty set.
    """
    documents, next_cursor = await DocumentService(profile).page(
        limit=limit,
        cursor=cursor,
        status=status_filter,
        label_id=label_id,
        unlabelled=unlabelled,
        search=search,
    )
    return DocumentPage(
        items=[DocumentResponse.model_validate(document) for document in documents],
        next_cursor=next_cursor,
    )


@router.get("/documents/{document_id}")
async def get_document(document_id: UUID, profile: CurrentProfile) -> DocumentResponse:
    return DocumentResponse.model_validate(await DocumentService(profile).get(document_id))


@router.get("/documents/{document_id}/file")
async def download_document(document_id: UUID, profile: CurrentProfile) -> FileResponse:
    """The original bytes, for the citation viewer F7 needs.

    Authorised by exactly the same read RLS applies to the metadata: a document that does
    not appear in the list is a 404 here too, never a 403, because the difference between
    them confirms that it exists.
    """
    document = await DocumentService(profile).get(document_id)
    path = DocumentStorage().path_for(profile.context.tenant_id, document.sha256)
    if not path.exists():
        # A row whose file is missing. Possible by design — the upload commits the row
        # first, so a crash between the two leaves this state rather than an orphan file
        # nobody can find. Reported as missing rather than as a 500.
        raise NotFoundError(f"the stored file for {document_id} is missing")
    return FileResponse(path, media_type="application/pdf", filename=document.filename)


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(requires_any(DELETE_OWN, DELETE_ANY))],
)
async def delete_document(document_id: UUID, profile: CurrentProfile) -> None:
    """Physical deletion, per RF-03.

    The gate admits either permission and the service decides which documents each one
    actually reaches. `delete.own` holders get a 403 on someone else's upload — which does
    disclose that the document exists, but only to a caller who can already see it in the
    list, so it discloses nothing they did not have.
    """
    await DocumentService(profile).delete(document_id)


def _reject_obviously_oversized(request: Request) -> None:
    """A cheap refusal before the body is read.

    Not the real limit — `Content-Length` is a claim the client makes, and the
    authoritative check runs on bytes actually received. This one exists because Starlette
    has already spooled the whole body to a temporary file by the time the handler runs,
    so an honest client announcing 4 GB should be told no before that happens.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.max_file_bytes:
        raise LimitExceededError(f"file exceeds the {settings.max_file_bytes} byte limit")


async def _stream(file: UploadFile) -> AsyncIterator[bytes]:
    while data := await file.read(CHUNK_BYTES):
        yield data
