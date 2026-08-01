from uuid import UUID

from sqlalchemy import select

from app.common.repositories.base import ScopedRepository
from app.features.auth.model import RolePermission, User, UserRole
from app.features.labels.model import RoleLabel


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
