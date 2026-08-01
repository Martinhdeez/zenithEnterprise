from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.common.exceptions import ZenithError
from app.core.database import verify_rls_active
from app.core.logging import configure_logging

configure_logging()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    # If RLS does not apply to this connection, the process must not serve traffic.
    await verify_rls_active()
    yield


app = FastAPI(title="Zenith Enterprise", version="0.1.0", lifespan=lifespan)


@app.exception_handler(ZenithError)
async def handle_domain_error(_: Request, exc: ZenithError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message},
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
