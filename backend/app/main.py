from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.common.exceptions import ZenithError
from app.common.problem import problem
from app.core.database import verify_rls_active
from app.core.hardware import active as active_profile
from app.core.logging import configure_logging
from app.core.request_context import RequestContextMiddleware
from app.features.admin.router import router as admin_router
from app.features.auth.router import router as auth_router
from app.features.documents.router import router as documents_router
from app.features.ingestion.tasks import app as procrastinate_app
from app.features.labels.router import router as labels_router
from app.features.query.router import router as query_router
from app.features.retrieval.router import router as search_router
from app.features.tenancy.router import router as tenancy_router

# Import-only, and load-bearing rather than tidiness: `app.models` is the one place every
# table gets registered on `Base.metadata` (Alembic's target). Nothing in this file uses
# the names it imports, but without this line the API process only ends up knowing about
# whatever tables its *own* import graph happens to touch — `tenancy_router` reaches
# `tenancy.status`, never `tenancy.service`, so `Tenant` was never imported and every FK
# to `tenants` failed at first flush with `NoReferencedTableError`, on any endpoint that
# happened to be the first to need it: `POST /labels`, `DELETE /documents/{id}`. A model
# never referenced from a router is exactly the case this import exists to cover.
from app.models import Base as _Base  # noqa: F401  # pyright: ignore[reportUnusedImport]

configure_logging()
log = structlog.get_logger()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    # If RLS does not apply to this connection, the process must not serve traffic.
    await verify_rls_active()
    # And if the hardware profile is not one we know, fail here rather than at the first
    # embedding request, at three in the morning, on a customer's server.
    active_profile()
    # `defer_async` needs its own pool open before the first call, or it raises
    # `AppNotOpen` — which `enqueue_ingestion` deliberately treats as non-fatal to the
    # upload (a stored document a requeue can recover beats a lost upload), so this was
    # failing silently on every single upload rather than loudly on the first one. The
    # worker process opens its own copy of this same `App` independently; this is the
    # API's, for writing jobs rather than running them.
    async with procrastinate_app.open_async():
        yield


app = FastAPI(
    title="Zenith Enterprise",
    version="0.1.0",
    lifespan=lifespan,
    description=(
        "On-premise enterprise knowledge retrieval.\n\n"
        "**Errors** follow RFC 7807 Problem Details (`application/problem+json`). Every "
        "failure carries `type`, `title`, `status` and `detail`; `code` and `message` are "
        "retained for clients written against the original shape.\n\n"
        "**Isolation** is enforced by Postgres row-level security, not by query filters. A "
        "resource another tenant owns is absent rather than forbidden, so a 404 rather "
        "than a 403 — the difference between them would confirm that it exists."
    ),
)

# Outermost, so a `trace_id` exists before anything else can log. Raw ASGI rather than
# BaseHTTPMiddleware, which buffers the response body and would defeat /query/stream.
app.add_middleware(RequestContextMiddleware)


@app.exception_handler(ZenithError)
async def handle_domain_error(request: Request, exc: ZenithError) -> JSONResponse:
    """Every domain error, as RFC 7807 Problem Details.

    `Retry-After` is forwarded when the error carries one. A 429 that says "stop" without
    saying "until when" is retried immediately, which makes the overload it was reporting
    worse — mvp.md 2.12 requires the header for that reason.

    F14 claimed this in its commit message and did not ship it: the edit ran from the wrong
    directory, wrote nothing, and the throttle tests asserted on the exception rather than
    on the response, so nothing failed. It is here now, and
    `test_a_rate_limit_carries_retry_after_in_both_places` asserts the response.
    """
    retry_after = getattr(exc, "retry_after", None)
    return problem(
        status=exc.status_code,
        code=exc.code,
        detail=exc.message,
        instance=request.url.path,
        headers={"Retry-After": str(retry_after)} if retry_after else None,
        # A machine-readable member alongside the header, per RFC 7807 §3.2: a client that
        # already parsed the body should not reach back into headers to learn when to retry.
        **({"retry_after": retry_after} if retry_after else {}),
    )


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Everything that is not a domain error, given the same shape and no detail.

    Without this, an unhandled exception becomes Starlette's default 500, whose body
    depends on how the app is served — plain text here, a traceback if someone leaves
    `debug=True` on a customer's server. A client cannot parse a response whose shape
    changes with the deployment, and a traceback names our file paths, our query text and
    occasionally the row that caused it.

    The message is deliberately fixed and useless to the caller. What they need is the
    correlation with the log line, and what the log line has is everything: the traceback,
    the path, and the method. Deciding what is safe to reveal per-exception is a judgement
    nobody makes correctly at three in the morning, so this makes it once.
    """
    log.exception(
        "unhandled_error",
        path=request.url.path,
        method=request.method,
        error=type(exc).__name__,
    )
    return problem(
        status=500,
        code="internal_error",
        detail="An unexpected error occurred.",
        instance=request.url.path,
    )


app.include_router(auth_router)
app.include_router(labels_router)
app.include_router(documents_router)
app.include_router(search_router)
app.include_router(query_router)
app.include_router(tenancy_router)
app.include_router(admin_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
