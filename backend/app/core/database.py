from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from sqlalchemy import DateTime, MetaData, func, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, mapped_column

from app.core.config import settings

if TYPE_CHECKING:
    from app.features.tenancy.context import TenantContext

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_N_name)s",
        "pk": "pk_%(table_name)s",
    }
)


class Base(DeclarativeBase):
    metadata = metadata


uuid_pk = Annotated[
    UUID,
    mapped_column(PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
]
uuid_col = Annotated[UUID, mapped_column(PgUUID(as_uuid=True))]
created_at = Annotated[
    datetime,
    mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False),
]


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_owner_engine: AsyncEngine | None = None
_owner_session_factory: async_sessionmaker[AsyncSession] | None = None


def configure_engine(url: str, pool_size: int | None = None) -> None:
    """Set the application engine. Tests point this at their throwaway container."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.sync_engine.dispose()
    _engine = create_async_engine(
        url,
        pool_size=pool_size or settings.api_pool_size,
        max_overflow=0,
        pool_pre_ping=True,
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


def configure_owner_engine(url: str) -> None:
    global _owner_engine, _owner_session_factory
    if _owner_engine is not None:
        _owner_engine.sync_engine.dispose()
    # Small pool on purpose: this connection bypasses RLS, and the operations that
    # legitimately need it are rare and sequential.
    _owner_engine = create_async_engine(url, pool_size=2, max_overflow=0, pool_pre_ping=True)
    _owner_session_factory = async_sessionmaker(_owner_engine, expire_on_commit=False)


def get_engine() -> AsyncEngine:
    if _engine is None:
        configure_engine(settings.database_url)
    assert _engine is not None
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        configure_engine(settings.database_url)
    assert _session_factory is not None
    return _session_factory


def get_owner_session_factory() -> async_sessionmaker[AsyncSession]:
    if _owner_session_factory is None:
        configure_owner_engine(settings.database_owner_url)
    assert _owner_session_factory is not None
    return _owner_session_factory


async def set_rls_context(session: AsyncSession, context: "TenantContext") -> None:
    await session.execute(
        text("SELECT set_config('zenith.tenant_id', :tenant, true)"),
        {"tenant": str(context.tenant_id)},
    )
    await session.execute(
        text("SELECT set_config('zenith.label_ids', :labels, true)"),
        {"labels": ",".join(str(label) for label in context.label_ids)},
    )
    await session.execute(text(f"SET LOCAL statement_timeout = {settings.statement_timeout_ms}"))
    # Recorded on the session so the base repository can demand a context without
    # paying an extra round trip to check for one.
    session.info["context"] = context


async def verify_rls_active() -> None:
    """Check on startup that the policies actually apply to this connection.

    The schema does not use FORCE ROW LEVEL SECURITY, because the owner has to be
    able to create the first tenant before any context exists. The trade-off is that
    pointing the application at the owner's credentials would disable isolation
    **silently**, which is the worst possible failure mode for a multi-tenant system.
    So it is checked here: with no context set, `tenants` must return zero rows.

    Every process that opens application sessions must call this before serving,
    not just the API. A worker writing chunks with RLS disabled is as dangerous as
    an API reading them.
    """
    async with get_session_factory()() as session:
        visible = await session.scalar(text("SELECT count(*) FROM tenants"))
        current_user = await session.scalar(text("SELECT current_user"))
        if visible:
            raise RuntimeError(
                f"RLS is not active for user '{current_user}': {visible} tenants are "
                "visible with no context set. The application must connect as "
                "`zenith_app`, never as the schema owner."
            )


@asynccontextmanager
async def tenant_session(context: "TenantContext") -> AsyncGenerator[AsyncSession]:
    """Session with the RLS context pinned for the transaction.

    Policies read `zenith.tenant_id` and `zenith.label_ids`. With no context, nothing
    is visible: the failure is closed. Isolation is guaranteed by the database, not
    by the discipline of whoever writes the query.

    This is the only supported way for application code to reach the database.
    """
    async with get_session_factory()() as session, session.begin():
        await set_rls_context(session, context)
        yield session


@asynccontextmanager
async def owner_session() -> AsyncGenerator[AsyncSession]:
    """Session as the schema owner. **Bypasses RLS entirely.**

    It exists for the operations that cannot happen inside a tenant context because
    no context exists yet: creating the first tenant, and migrations. Nothing served
    over HTTP may use it.

    Kept deliberately greppable — searching for `owner_session` is a complete audit
    of the bypass surface. If that search ever returns a request handler, that is a
    bug regardless of what the handler does.
    """
    async with get_owner_session_factory()() as session, session.begin():
        yield session
