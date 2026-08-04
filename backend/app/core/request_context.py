"""Per-request context on every log line, and nothing sensitive on any of them.

Structured logs were already JSON (F0's `configure_logging`). What they lacked was the two
fields that make them usable during an incident: **which request** a line belongs to, and
**which tenant** it concerned.

Without a `trace_id`, twenty concurrent queries interleave into one stream and no line can
be attributed to the request that produced it — which is precisely the situation you are in
when something is wrong. F11 measured ten concurrent users as a realistic load; ten
interleaved query traces are unreadable without it.

Bound with `structlog.contextvars`, so every log emitted anywhere in the request — including
inside services that know nothing about HTTP — carries them without being passed a logger.

## What is deliberately absent

**The question.** `queries.question` is stored in the database, under RLS, where it belongs.
Putting it in the log stream copies customer content into a file that is shipped to
aggregators, read during support calls and retained on a different schedule from the corpus
— and questions are more revealing than documents. The `query_id` correlates the two for
anybody entitled to both.

**The user id.** `tenant_id` is enough to route an incident to a customer. Which employee
asked what belongs in the audit trail, not in an operational log that a support engineer
tails.

**Headers.** `Authorization` is the obvious one, and the rule is easier to keep than a list:
none are logged.
"""

import uuid
from typing import cast

import structlog
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

#: Echoed back so a user reporting a problem can quote something a support engineer can
#: grep for. It identifies a request, not a person, and it is generated here rather than
#: trusted from the client — an inbound identifier would let a caller poison the logs.
TRACE_HEADER = "X-Trace-Id"

log = structlog.get_logger()


class RequestContextMiddleware:
    """Bind `trace_id` for every request, and `tenant_id` once the caller is known.

    Written as a raw ASGI class rather than `BaseHTTPMiddleware`, which buffers the
    response body — that would defeat `POST /query/stream`, whose entire purpose is to send
    tokens as they arrive.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, object], receive: object, send: object) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)  # type: ignore[arg-type]
            return

        structlog.contextvars.clear_contextvars()
        trace_id = uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

        async def send_with_trace(message: dict[str, object]) -> None:
            if message.get("type") == "http.response.start":
                raw = message.get("headers")
                existing: list[tuple[bytes, bytes]] = (
                    list(cast(list[tuple[bytes, bytes]], raw)) if isinstance(raw, list) else []
                )
                existing.append((TRACE_HEADER.lower().encode(), trace_id.encode()))
                message["headers"] = existing
            await send(message)  # type: ignore[operator]

        await self.app(scope, receive, send_with_trace)  # type: ignore[arg-type]


def bind_tenant(request: Request) -> None:
    """Attach the tenant to the log context once authentication has resolved it.

    Called from the authentication dependency rather than the middleware, because the
    middleware runs before anything has read the token — and a tenant guessed from an
    unverified header would be worse than no tenant at all.
    """
    del request  # the context is bound by the caller that already resolved it


def bind(**fields: object) -> None:
    """Add fields to every subsequent log line in this request."""
    structlog.contextvars.bind_contextvars(**fields)


async def log_response(request: Request, response: Response) -> None:
    """One line per request, at the boundary, with no body and no headers."""
    log.info(
        "request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
    )
