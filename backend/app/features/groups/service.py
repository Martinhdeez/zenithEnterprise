"""Managing groups, their members, and the labels they open.

Everything runs inside `tenant_session`, so a group belonging to another customer is not
forbidden — it is absent, and a request naming it is a 404 rather than a 403. The difference
between those two answers confirms the thing exists, which is the inference mvp.md 3.1
spends its whole budget preventing.

Nothing here decides access. It writes the rows that `UserRepository.label_ids` reads, and
that function plus the RLS policies are what actually enforce anything. Keeping the writer
and the enforcer separate is deliberate: an administration screen that could grant access
by any route other than the one the policies read would be a second access model nobody
tested.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

from app.common.exceptions import ConflictError, NotFoundError
from app.core.database import tenant_session
from app.features.auth.service import AccessProfile

#: Managing who is in which group is managing who reads what, so it sits behind the same
#: permission as roles rather than a weaker one of its own.
MANAGE = "roles.manage"


@dataclass(frozen=True, slots=True)
class Group:
    id: UUID
    name: str
    description: str | None
    #: How many users are in it. A group about to be edited is easier to reason about when
    #: you know whether anybody is standing behind the change.
    members: int
    #: The labels this group opens — to a member who also carries the clearance each one
    #: demands. Not a list of what any particular member can read.
    label_ids: list[UUID]


class GroupService:
    def __init__(self, profile: AccessProfile) -> None:
        self.profile = profile
        self.context = profile.context

    async def visible(self) -> list[Group]:
        async with tenant_session(self.context) as session:
            rows = await session.execute(
                text(
                    "SELECT g.id, g.name, g.description, "
                    "  coalesce(array_agg(DISTINCT gl.label_id) "
                    "    FILTER (WHERE gl.label_id IS NOT NULL), '{}') AS label_ids, "
                    "  (SELECT count(*) FROM user_groups ug WHERE ug.group_id = g.id) AS members "
                    "FROM groups g "
                    "LEFT JOIN group_labels gl ON gl.group_id = g.id "
                    "GROUP BY g.id, g.name, g.description ORDER BY g.name"
                )
            )
            return [
                Group(
                    id=row.id,
                    name=row.name,
                    description=row.description,
                    members=int(row.members),
                    label_ids=list(row.label_ids),
                )
                for row in rows
            ]

    async def create(self, name: str, description: str | None = None) -> Group:
        async with tenant_session(self.context) as session:
            existing = await session.scalar(
                text("SELECT 1 FROM groups WHERE name = :name"), {"name": name}
            )
            if existing:
                # Caught here rather than left to the unique constraint so the caller gets
                # a sentence instead of a driver error naming a constraint.
                raise ConflictError(f"a group called {name!r} already exists")
            group_id = await session.scalar(
                text(
                    "INSERT INTO groups (tenant_id, name, description) "
                    "VALUES (:t, :name, :description) RETURNING id"
                ),
                {"t": self.context.tenant_id, "name": name, "description": description},
            )
        return await self._one(UUID(str(group_id)))

    async def rename(self, group_id: UUID, name: str, description: str | None) -> Group:
        await self._must_exist(group_id)
        async with tenant_session(self.context) as session:
            await session.execute(
                text("UPDATE groups SET name = :name, description = :d WHERE id = :g"),
                {"name": name, "d": description, "g": group_id},
            )
        return await self._one(group_id)

    async def delete(self, group_id: UUID) -> None:
        """Deleting a group removes access rather than transferring it.

        The cascade takes `user_groups` and `group_labels` with it, so every member loses
        whatever that group opened. That is the intended meaning of deleting a group, and
        it is why the members count is on the read model — so the screen can say how many
        people this is about to affect.
        """
        await self._must_exist(group_id)
        async with tenant_session(self.context) as session:
            await session.execute(text("DELETE FROM groups WHERE id = :g"), {"g": group_id})

    async def set_labels(self, group_id: UUID, label_ids: list[UUID]) -> Group:
        """Replace the labels this group opens.

        Replace rather than add/remove: a caller sending the full set knows what the group
        will open afterwards, while a patch makes the result depend on state they did not
        read. Same reasoning as `RoleService.set_permissions`.
        """
        await self._must_exist(group_id)
        async with tenant_session(self.context) as session:
            known = set(
                await session.scalars(
                    text("SELECT id FROM access_labels WHERE id = ANY(:ids)"),
                    {"ids": [str(label_id) for label_id in label_ids]},
                )
            )
            unknown = [str(i) for i in label_ids if i not in known]
            if unknown:
                # Reached through RLS, so a label in another tenant is simply not here.
                raise NotFoundError(f"no label(s): {', '.join(unknown)}")

            await session.execute(
                text("DELETE FROM group_labels WHERE group_id = :g"), {"g": group_id}
            )
            for label_id in sorted(set(label_ids), key=str):
                await session.execute(
                    text(
                        "INSERT INTO group_labels (group_id, label_id) VALUES (:g, :l) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"g": group_id, "l": label_id},
                )
        return await self._one(group_id)

    async def set_members(self, group_id: UUID, user_ids: list[UUID]) -> Group:
        await self._must_exist(group_id)
        async with tenant_session(self.context) as session:
            known = set(
                await session.scalars(
                    text("SELECT id FROM users WHERE id = ANY(:ids)"),
                    {"ids": [str(user_id) for user_id in user_ids]},
                )
            )
            unknown = [str(i) for i in user_ids if i not in known]
            if unknown:
                raise NotFoundError(f"no user(s): {', '.join(unknown)}")

            await session.execute(
                text("DELETE FROM user_groups WHERE group_id = :g"), {"g": group_id}
            )
            for user_id in sorted(set(user_ids), key=str):
                await session.execute(
                    text(
                        "INSERT INTO user_groups (user_id, group_id) VALUES (:u, :g) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"u": user_id, "g": group_id},
                )
        return await self._one(group_id)

    async def set_user_groups(self, user_id: UUID, group_ids: list[UUID]) -> None:
        """The same relation from the user's side, for the screen that edits one person."""
        async with tenant_session(self.context) as session:
            if not await session.scalar(text("SELECT 1 FROM users WHERE id = :u"), {"u": user_id}):
                raise NotFoundError(f"no user {user_id}")

            known = {group.id for group in await self.visible()}
            unknown = [str(i) for i in group_ids if i not in known]
            if unknown:
                raise NotFoundError(f"no group(s): {', '.join(unknown)}")

            await session.execute(
                text("DELETE FROM user_groups WHERE user_id = :u"), {"u": user_id}
            )
            for group_id in group_ids:
                await session.execute(
                    text(
                        "INSERT INTO user_groups (user_id, group_id) VALUES (:u, :g) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"u": user_id, "g": group_id},
                )

    async def of_user(self, user_id: UUID) -> list[UUID]:
        async with tenant_session(self.context) as session:
            return list(
                await session.scalars(
                    text("SELECT group_id FROM user_groups WHERE user_id = :u"), {"u": user_id}
                )
            )

    async def _must_exist(self, group_id: UUID) -> None:
        if group_id not in {group.id for group in await self.visible()}:
            raise NotFoundError(f"no group {group_id}")

    async def _one(self, group_id: UUID) -> Group:
        return next(group for group in await self.visible() if group.id == group_id)
