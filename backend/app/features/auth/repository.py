from uuid import UUID

from sqlalchemy import func, select

from app.common.repositories.base import ScopedRepository
from app.features.auth.model import Role, RolePermission, User, UserRole
from app.features.documents.model import Document
from app.features.groups.model import GroupLabel, UserGroup
from app.features.labels.model import AccessLabel, RoleLabel
from app.features.tenancy.model import ACTIVE, Tenant


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
        """Every label the user reaches, by either of the two routes.

        This is the value RLS is handed for the rest of the request. Too many labels
        here is a data leak that the policies will enforce with complete confidence,
        which is why it is resolved in one place and tested directly.

        **Grant**: somebody put the label in `role_labels` for a role this user holds. The
        original route, unconditional, and still the only one that opens a label belonging
        to no group.

        **Group and clearance**: the label is mapped to a group the user is in, *and* the
        user's clearance is at or above what the label demands. Both, not either. Being in
        Finance does not by itself open `finance/confidential`, and being senior does not
        put anybody in Finance — those are the horizontal and vertical halves, and a model
        where one implies the other is not an access model.

        A label mapped to no group is unreachable by the second route no matter whose
        clearance is what, so every label that predates groups behaves exactly as it did.

        The two routes are a union rather than a precedence: a clearance cannot take away a
        grant and a grant cannot take away a clearance. Anything else would mean the order
        rows happened to be written in decides what somebody can read.
        """
        granted = (
            select(RoleLabel.label_id)
            .join(UserRole, UserRole.role_id == RoleLabel.role_id)
            .where(UserRole.user_id == user_id)
        )
        # `max` over the roles held rather than a sum: holding two roles at level 3 is
        # level 3. NULL when the user holds no role, which makes the comparison below false
        # rather than raising — so a user with no roles reaches nothing this way.
        clearance = (
            select(func.max(Role.priority_level))
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id)
            .scalar_subquery()
        )
        through_group = (
            select(GroupLabel.label_id)
            .join(UserGroup, UserGroup.group_id == GroupLabel.group_id)
            .join(AccessLabel, AccessLabel.id == GroupLabel.label_id)
            .where(
                UserGroup.user_id == user_id,
                AccessLabel.priority_level <= clearance,
            )
        )
        return tuple(await self.session.scalars(granted.union(through_group)))

    async def tenant_status(self) -> str:
        """The lifecycle state of the tenant this session is scoped to.

        No `WHERE tenant_id` — there is no need. `tenants` carries the policy
        `id = zenith_current_tenant()`, so this session can see exactly one row, and asking
        for it by filter would restate a rule the database is already enforcing.
        """
        return await self.session.scalar(select(Tenant.status)) or ACTIVE

    async def is_system_admin(self, user_id: UUID) -> bool:
        """Authority above every tenant.

        Readable here, and only readable: migration 0010 narrows `zenith_app`'s UPDATE
        grant on `users` to a column list that omits this one, so no code path reachable
        from a request can set it.
        """
        return bool(
            await self.session.scalar(select(User.is_system_admin).where(User.id == user_id))
        )

    async def email(self, user_id: UUID) -> str:
        """The caller's own address, for stamping onto audit rows.

        Read here rather than looked up when an event is written: `profile()` already opens
        a session and reads this user's row, so it costs nothing, and an audit record must
        never depend on a second query that could fail after the change it describes has
        already been committed.
        """
        return await self.session.scalar(select(User.email).where(User.id == user_id)) or ""

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
