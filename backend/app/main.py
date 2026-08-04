from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.common.exceptions import ZenithError
from app.core.database import verify_rls_active
from app.core.hardware import active as active_profile
from app.core.logging import configure_logging
from app.features.auth.router import router as auth_router
from app.features.documents.router import router as documents_router
from app.features.labels.router import router as labels_router
from app.features.query.router import router as query_router
from app.features.retrieval.router import router as search_router

configure_logging()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    # If RLS does not apply to this connection, the process must not serve traffic.
    await verify_rls_active()
    # And if the hardware profile is not one we know, fail here rather than at the first
    # embedding request, at three in the morning, on a customer's server.
    active_profile()
    yield


app = FastAPI(title="Zenith Enterprise", version="0.1.0", lifespan=lifespan)


@app.exception_handler(ZenithError)
async def handle_domain_error(_: Request, exc: ZenithError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message},
    )


app.include_router(auth_router)
app.include_router(labels_router)
app.include_router(documents_router)
app.include_router(search_router)
app.include_router(query_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
