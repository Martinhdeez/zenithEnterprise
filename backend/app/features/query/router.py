from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

from app.features.auth.dependencies import CurrentProfile, requires
from app.features.generation.service import EXECUTE, Answer, AnswerService
from app.features.query.schemas import (
    CitationResponse,
    ConsultedResponse,
    QueryRequest,
    QueryResponse,
)

router = APIRouter(tags=["query"])


@router.post(
    "/query",
    operation_id="askQuestion",
    summary="Ask a question and receive a fully validated, cited answer",
    responses={
        403: {"description": "Missing query.execute, or a label the caller does not hold"},
        503: {"description": "No language model is configured, or it could not be reached"},
    },
    dependencies=[Depends(requires(EXECUTE))],
)
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
    return _rendered(await AnswerService(profile).answer(request.question, request.labels))


def _rendered(result: Answer) -> QueryResponse:
    """One mapping from the domain answer to the wire, used by both endpoints.

    Shared deliberately: the streaming `result` event and the buffered response must be the
    same shape, or a client would need two parsers for one answer and the two would drift
    the first time a field was added.
    """
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


@router.post(
    "/query/stream",
    operation_id="askQuestionStreaming",
    summary="Ask a question and receive the answer as it is written",
    response_class=EventSourceResponse,
    responses={
        200: {
            "description": (
                "Server-Sent Events. `token` events carry text as the model writes it; a "
                "final `result` event carries the authoritative answer, its citations and "
                "`abstained`.\n\n"
                "**A client must not present a streamed answer as final until the `result` "
                "event arrives, and must replace what it displayed if `abstained` is "
                "true.** An invalid citation marker never appears in a `token` event — that "
                "guarantee holds while streaming. Whether the answer cites anything valid "
                "*at all* can only be known at the end, which is the guarantee this "
                "endpoint trades for time-to-first-token. `POST /query` does not trade it."
            ),
            "content": {"text/event-stream": {}},
        },
        403: {"description": "Missing query.execute, or a label the caller does not hold"},
        503: {"description": "No language model is configured, or it could not be reached"},
    },
    dependencies=[Depends(requires(EXECUTE))],
)
async def ask_streaming(profile: CurrentProfile, request: QueryRequest) -> EventSourceResponse:
    """Server-Sent Events rather than WebSockets.

    One direction, text, over plain HTTP. SSE survives corporate proxies that break
    WebSocket upgrades and needs no second protocol in the deployment. An on-premise product
    is installed behind infrastructure nobody warned us about, and the protocol that asks
    least of it wins.

    Events are named rather than bare data lines, so a client can ignore what it does not
    recognise and a fourth event type can be added without breaking it.
    """
    service = AnswerService(profile)

    async def events() -> AsyncIterator[dict[str, str]]:
        async for piece in service.stream(request.question, request.labels):
            if piece.token is not None:
                yield {"event": "token", "data": piece.token}
            elif piece.result is not None:
                yield {"event": "result", "data": _rendered(piece.result).model_dump_json()}

    return EventSourceResponse(events())
