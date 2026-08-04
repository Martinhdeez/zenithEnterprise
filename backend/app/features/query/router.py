from fastapi import APIRouter, Depends

from app.features.auth.dependencies import CurrentProfile, requires
from app.features.generation.service import EXECUTE, AnswerService
from app.features.query.schemas import (
    CitationResponse,
    ConsultedResponse,
    QueryRequest,
    QueryResponse,
)

router = APIRouter(tags=["query"])


@router.post("/query", dependencies=[Depends(requires(EXECUTE))])
async def ask(profile: CurrentProfile, request: QueryRequest) -> QueryResponse:
    """Ask a question of the corpus this caller is allowed to read.

    POST rather than GET, unlike `/search`: this one writes. Every call records a row in
    `queries` and one per citation in `query_citations` — observability today and the audit
    trail in iteration 4 — and an endpoint with side effects does not belong behind a verb
    that proxies and browsers are entitled to retry.

    Every citation in the response resolves to a passage that was in the shortlist RLS
    released to this caller. The model is shown numbered passages and never a chunk id, so
    it cannot name one it was not given; markers naming a passage that was not sent are
    stripped before the answer leaves this process.
    """
    result = await AnswerService(profile).answer(request.question, request.labels)
    return QueryResponse(
        query_id=result.query_id,
        answer=result.answer,
        citations=[
            CitationResponse(
                marker=citation.marker,
                chunk_id=citation.chunk_id,
                document_id=citation.document_id,
                filename=citation.filename,
                page_num=citation.page_num,
                text=citation.text,
                bboxes=citation.bboxes,
            )
            for citation in result.citations
        ],
        abstained=result.abstained,
        consulted=[
            ConsultedResponse(
                document_id=hit.document_id, filename=hit.filename, page_num=hit.page_num
            )
            for hit in result.consulted
        ],
        model=result.model,
        degraded=result.degraded,
        reason=result.reason,
        took_retrieval_ms=result.took_retrieval_ms,
        took_generation_ms=result.took_generation_ms,
    )
