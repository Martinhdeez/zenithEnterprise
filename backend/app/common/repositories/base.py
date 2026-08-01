from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import MissingTenantContextError
from app.core.database import Base

if TYPE_CHECKING:
    from app.features.tenancy.context import TenantContext


class ScopedRepository[ModelT: Base]:
    """Base repository for tables holding customer data.

    It exists for one reason: to guarantee that no query runs without an RLS context.
    Postgres policies already close the door, but with no context they silently
    return zero rows, and a silent zero is hard to debug. Here it fails loudly, in
    the right place.

    Named for what it does — scoping to a context — rather than for the tenancy
    feature, so `TenantRepository` can mean the repository of tenants and nothing
    else.
    """

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        if "context" not in session.info:
            raise MissingTenantContextError("session has no RLS context: use `tenant_session`")
        self.session = session

    @property
    def context(self) -> "TenantContext":
        return self.session.info["context"]

    @property
    def tenant_id(self) -> UUID:
        return self.context.tenant_id

    def query(self) -> Select[tuple[ModelT]]:
        return select(self.model)

    async def get(self, entity_id: UUID) -> ModelT | None:
        return await self.session.get(self.model, entity_id)

    async def list(self) -> list[ModelT]:
        result = await self.session.scalars(self.query())
        return list(result)

    async def add(self, entity: ModelT) -> ModelT:
        self.session.add(entity)
        await self.session.flush()
        return entity

    async def delete(self, entity: ModelT) -> None:
        await self.session.delete(entity)
        await self.session.flush()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not hasattr(cls, "model"):
            raise TypeError(f"{cls.__name__} must declare `model`")
