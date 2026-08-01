from uuid import UUID

from sqlalchemy import delete, func, select, update

from app.common.repositories.base import ScopedRepository
from app.features.auth.model import Role
from app.features.documents.model import Document
from app.features.labels.model import AccessLabel, DocumentLabel, RoleLabel


class LabelRepository(ScopedRepository[AccessLabel]):
    model = AccessLabel

    async def by_name(self, name: str) -> AccessLabel | None:
        return await self.session.scalar(select(AccessLabel).where(AccessLabel.name == name))

    async def reachable(self) -> list[AccessLabel]:
        """Only the labels the current context reaches.

        A label name is itself a disclosure — "Project Titan acquisition" says something
        merely by existing. RLS on `access_labels` is tenant-wide, because the rows have to
        be readable for the joins that resolve a context, so this restriction is
        application-level. That makes it exactly the kind of rule that gets forgotten,
        which is why it has its own test.
        """
        if not self.context.label_ids:
            return []
        statement = select(AccessLabel).where(AccessLabel.id.in_(self.context.label_ids))
        return list(await self.session.scalars(statement.order_by(AccessLabel.name)))

    async def all_in_tenant(self) -> list[AccessLabel]:
        """Every label. For holders of `labels.manage`: managing a set you cannot
        enumerate is not management."""
        return list(await self.session.scalars(select(AccessLabel).order_by(AccessLabel.name)))

    async def documents_using(self, label_id: UUID) -> int:
        """How many documents carry this label.

        Deleting a label that is a document's only label leaves that document with an
        empty array — and an empty array means visible to the entire tenant. A delete that
        widens access, silently, through a cascade. This is what the guard counts.
        """
        return (
            await self.session.scalar(
                select(func.count())
                .select_from(DocumentLabel)
                .where(DocumentLabel.label_id == label_id)
            )
            or 0
        )

    async def clear_default(self) -> None:
        await self.session.execute(
            update(AccessLabel).where(AccessLabel.is_default).values(is_default=False)
        )

    async def default(self) -> AccessLabel | None:
        return await self.session.scalar(select(AccessLabel).where(AccessLabel.is_default))

    async def role_exists(self, role_id: UUID) -> bool:
        return await self.session.scalar(select(Role.id).where(Role.id == role_id)) is not None

    async def document_exists(self, document_id: UUID) -> bool:
        return (
            await self.session.scalar(select(Document.id).where(Document.id == document_id))
            is not None
        )

    async def label_ids_in_tenant(self, label_ids: list[UUID]) -> set[UUID]:
        """Which of these labels exist in the caller's tenant.

        RLS would reject a foreign label on write anyway, but as a constraint violation
        rather than as something the API can explain. The caller gets told which id was
        wrong instead of a 500.
        """
        if not label_ids:
            return set()
        return set(
            await self.session.scalars(select(AccessLabel.id).where(AccessLabel.id.in_(label_ids)))
        )

    async def set_role_labels(self, role_id: UUID, label_ids: list[UUID]) -> None:
        await self.session.execute(delete(RoleLabel).where(RoleLabel.role_id == role_id))
        self.session.add_all(
            RoleLabel(role_id=role_id, label_id=label_id) for label_id in label_ids
        )
        await self.session.flush()

    async def set_document_labels(self, document_id: UUID, label_ids: list[UUID]) -> None:
        """Replace a document's labels.

        `documents.label_ids` is not touched here: the trigger installed by migration 0003
        recomputes it. Doing it in both places is how the copy drifts from its source, and
        the copy is what RLS actually reads.
        """
        await self.session.execute(
            delete(DocumentLabel).where(DocumentLabel.document_id == document_id)
        )
        self.session.add_all(
            DocumentLabel(document_id=document_id, label_id=label_id) for label_id in label_ids
        )
        await self.session.flush()
