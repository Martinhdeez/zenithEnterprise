from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sse_starlette.sse import EventSourceResponse

from app.features.auth.access.dependencies import CurrentProfile, requires, requires_any
from app.features.generation.answering.conversation import Turn
from app.features.generation.service import EXECUTE, Answer, AnswerService
from app.features.query.history import ANY, OWN, HistoryService
from app.features.query.schemas import (
    CitationResponse,
    ConsultedResponse,
    HistoryEntryResponse,
    HistoryResponse,
    QueryRequest,
    QueryResponse,
)
from app.features.query.throttle import RateLimit

router = APIRouter(tags=["query"])


@router.post(
    "/query",
    operation_id="askQuestion",
    summary="Ask a question and receive a fully validated, cited answer",
    responses={
        403: {"description": "Missing query.execute, or a label the caller does not hold"},
        429: {"description": "Too many questions from this user or organisation"},
        503: {"description": "No language model is configured, or it could not be reached"},
    },
    # The permission says *may* you ask; the limit says *how often*. Both, because the
    # expensive thing here is the model call and a permission cannot bound it — F9 measured
    # ~9 seconds of model time per answer, and F11 measured what ten concurrent requests do
    # to four cores.
    dependencies=[Depends(requires(EXECUTE)), RateLimit],
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
    return _rendered(
        await AnswerService(profile).answer(
            request.question, request.labels, _thread(request), request.documents
        )
    )


def _thread(request: QueryRequest) -> list[Turn]:
    """The client's thread, in the shape the generator wants."""
    return [Turn(question=turn.question, answer=turn.answer) for turn in request.history]


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
        429: {"description": "Too many questions from this user or organisation"},
        503: {"description": "No language model is configured, or it could not be reached"},
    },
    # More important here than on `/query`, not less: a stream holds a worker and a socket
    # for the whole answer, so thirty tabs left open on a dashboard that retries is an outage
    # nobody had to be malicious to cause.
    dependencies=[Depends(requires(EXECUTE)), RateLimit],
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
        async for piece in service.stream(
            request.question, request.labels, _thread(request), request.documents
        ):
            if piece.token is not None:
                yield {"event": "token", "data": piece.token}
            elif piece.result is not None:
                yield {"event": "result", "data": _rendered(piece.result).model_dump_json()}

    return EventSourceResponse(events())


@router.get(
    "/query/history",
    operation_id="listQueryHistory",
    summary="Past questions and the answers they received",
    responses={403: {"description": "Missing query.history.own and query.history.any"}},
    dependencies=[Depends(requires_any(OWN, ANY))],
)
async def history(
    profile: CurrentProfile,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
    cursor: Annotated[str | None, Query(description="From a previous page.")] = None,
    search: Annotated[
        str | None, Query(max_length=200, description="Match against the question text.")
    ] = None,
    mine: Annotated[
        bool, Query(description="Only your own questions, even if you may read everyone's.")
    ] = False,
    unanswered: Annotated[bool, Query(description="Only questions no document answered.")] = False,
) -> HistoryResponse:
    """Whose history is returned is decided by the caller's permissions, never by a
    parameter.

    `query.history.any` reads the whole tenant's; `query.history.own` reads only the
    caller's. That distinction is enforced by migration 0005's policy rather than here,
    because the questions people ask — *"what is my severance?"* — are more revealing than
    the documents they read.

    The three parameters below narrow that set and can never widen it. `mine=true` is
    somebody who may read everyone's asking to look away from it; there is no parameter for
    the opposite, because that is a fact about the caller rather than a request.
    """
    page = await HistoryService(profile).page(
        limit, cursor, search=search, mine_only=mine, unanswered_only=unanswered
    )
    return HistoryResponse(
        entries=[HistoryEntryResponse(**asdict(entry)) for entry in page.entries],
        next_cursor=page.next_cursor,
    )
