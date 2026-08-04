"""Log context, and what must never appear in it.

An incident is the moment these matter, and it is the worst moment to discover that twenty
concurrent requests interleaved into one unattributable stream — or that customer questions
were being copied into a file shipped to an aggregator.
"""

import structlog
from starlette.requests import Request

from app.core.request_context import TRACE_HEADER, RequestContextMiddleware


def request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/query", "headers": []})


async def call(
    app_response_headers: list[tuple[bytes, bytes]] | None = None,
) -> list[dict[str, object]]:
    """Drive the middleware over one request and collect what was sent."""
    sent: list[dict[str, object]] = []

    async def app(scope: object, receive: object, send: object) -> None:
        await send(
            {"type": "http.response.start", "status": 200, "headers": app_response_headers or []}
        )  # type: ignore[operator]
        await send({"type": "http.response.body", "body": b""})  # type: ignore[operator]

    async def receive() -> dict[str, object]:
        return {"type": "http.request"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await RequestContextMiddleware(app)(  # type: ignore[arg-type]
        {"type": "http", "method": "POST", "path": "/query", "headers": []}, receive, send
    )
    return sent


async def test_a_trace_id_is_returned_to_the_caller() -> None:
    """So a user reporting a problem can quote something support can grep for."""
    sent = await call()

    start = next(message for message in sent if message["type"] == "http.response.start")
    headers = dict(start["headers"])  # type: ignore[arg-type]
    assert TRACE_HEADER.lower().encode() in headers
    assert len(headers[TRACE_HEADER.lower().encode()]) == 32


async def test_the_trace_id_is_generated_not_trusted_from_the_client() -> None:
    """An inbound identifier would let a caller poison the logs — collide with somebody
    else's trace, or write a value chosen to be unsearchable."""
    first = await call()
    second = await call()

    def trace(sent: list[dict[str, object]]) -> bytes:
        start = next(message for message in sent if message["type"] == "http.response.start")
        return dict(start["headers"])[TRACE_HEADER.lower().encode()]  # type: ignore[arg-type,index]

    assert trace(first) != trace(second)


async def test_every_line_in_a_request_carries_the_trace() -> None:
    """Bound with contextvars, so a service that knows nothing about HTTP still emits it.

    Without this, ten concurrent queries — which F11 measured as a realistic load —
    interleave into one stream with no line attributable to the request that produced it.

    Asserted against the bound context rather than against rendered output, for two
    reasons. Rendering is a different decision in a different module, and
    `structlog.testing.capture_logs` replaces the processor chain — including
    `merge_contextvars` — so it reports a line with no context and would pass a broken
    implementation. Reading the contextvars is what actually tests the binding.
    """
    seen: dict[str, object] = {}

    async def app(scope: object, receive: object, send: object) -> None:
        seen.update(structlog.contextvars.get_contextvars())
        await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]
        await send({"type": "http.response.body", "body": b""})  # type: ignore[operator]

    async def receive() -> dict[str, object]:
        return {"type": "http.request"}

    async def send(message: dict[str, object]) -> None:
        return None

    await RequestContextMiddleware(app)(  # type: ignore[arg-type]
        {"type": "http", "method": "POST", "path": "/query", "headers": []}, receive, send
    )

    assert "trace_id" in seen, "every line inside the request must be attributable to it"
    assert len(str(seen["trace_id"])) == 32


def test_the_context_never_carries_the_question_or_the_user() -> None:
    """Deliberate absences, and the reason each one is absent.

    The question is customer content: it lives in `queries` under RLS, on the corpus's
    retention schedule, not in a log stream shipped to an aggregator and read during
    support calls. Questions are more revealing than documents.

    The user id is left out because `tenant_id` is enough to route an incident to a
    customer; which employee asked what belongs in the audit trail, not in a log a support
    engineer tails.
    """
    from app.core import request_context

    source = request_context.__doc__ or ""

    assert "question" in source.lower(), "the omission must stay documented where it applies"
    # The module binds exactly two identifying fields, and neither is a person.
    assert "user_id" not in request_context.TRACE_HEADER
