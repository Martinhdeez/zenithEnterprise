from uuid import UUID

from sqlalchemy import func, select

from app.common.repositories.base import ScopedRepository
from app.features.auth.model import Role, RolePermission, User, UserRole
from app.features.documents.model import Document
from app.features.labels.model import AccessLabel, RoleLabel
from app.features.tenancy.model import Tenant


class UserRepository(ScopedRepository[User]):
    """Reads inside a tenant context.

    Everything here is reachable with a context carrying no labels: labels gate
    `documents` and `chunks`, never the tables describing who someone is. That is what
    makes it possible to resolve a user's labels before knowing them.
    """

    model = User

    async def by_email(self, email: str) -> User | None:
        return await self.session.scalar(select(User).where(User.email == email))

    async def permission_codes(self, user_id: UUID) -> frozenset[str]:
        """The union over every role the user holds.

        Read from the database rather than compared against an enum, which is what
        makes a custom role configuration instead of a deployment (mvp.md 2.1).
        """
        statement = (
            select(RolePermission.permission_code)
            .join(UserRole, UserRole.role_id == RolePermission.role_id)
            .where(UserRole.user_id == user_id)
        )
        return frozenset(await self.session.scalars(statement))

    async def label_ids(self, user_id: UUID) -> tuple[UUID, ...]:
        """The union of the labels the user's roles reach.

        This is the value RLS is handed for the rest of the request. Too many labels
        here is a data leak that the policies will enforce with complete confidence,
        which is why it is resolved in one place and tested directly.
        """
        statement = (
            select(RoleLabel.label_id)
            .join(UserRole, UserRole.role_id == RoleLabel.role_id)
            .where(UserRole.user_id == user_id)
            .distinct()
        )
        return tuple(await self.session.scalars(statement))

    async def role_names(self, user_id: UUID) -> list[str]:
        """The roles this user holds, by the names their administrator chose.

        For the profile screen. Permission codes are the authority and are already
        returned beside these; a role name is what somebody recognises about themselves.
        """
        statement = (
            select(Role.name)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id)
            .order_by(Role.name)
        )
        return list(await self.session.scalars(statement))

    async def label_names(self, label_ids: tuple[UUID, ...]) -> list[str]:
        """Names for the labels a caller reaches.

        The profile shows these to answer the question this product prompts more than any
        other — "why can I not see the document my colleague can" — whose answer is always
        that the label behind it is not one of these. Ids alone cannot answer it.
        """
        if not label_ids:
            return []
        statement = (
            select(AccessLabel.name).where(AccessLabel.id.in_(label_ids)).order_by(AccessLabel.name)
        )
        return list(await self.session.scalars(statement))

    async def tenant_name(self) -> str | None:
        return await self.session.scalar(select(Tenant.name))

    async def documents_uploaded(self, user_id: UUID) -> int:
        """How many documents this person put into the corpus.

        RLS-scoped like everything else here: it counts what they can still see, so a
        document filed under a label their role later lost is not in the number.
        """
        return (
            await self.session.scalar(
                select(func.count()).select_from(Document).where(Document.uploaded_by == user_id)
            )
            or 0
        )
