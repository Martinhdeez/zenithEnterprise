"""Who is in this tenant — the list an administrator edits access from.

The product could invite users and assign them roles from the very first release, and could
never *see* them: `POST /users/invite` returned one person and nothing listed the rest. That
was survivable while roles were assigned at the moment of invitation, and stopped being so
the moment groups arrived, because a group's membership is edited long after anybody was
invited.

Everything runs inside `tenant_session`, so this is the tenant's own directory and nothing
else — a user in another customer is not filtered out here, they are absent from the query
by policy. Deliberately not paginated: a tenant's staff list is not a corpus, and a screen
that assigns groups needs everybody on it at once to be usable at all. If a customer ever
arrives with ten thousand staff, this grows a cursor like `documents` did.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

from app.core.database import tenant_session
from app.features.auth.service import AccessProfile

#: The same permission that guards editing a user. Seeing the staff list and their access is
#: administration, not something every member needs.
MANAGE = "users.manage"


@dataclass(frozen=True, slots=True)
class Member:
    id: UUID
    email: str
    #: What they chose to be called, or None — the screen falls back to the address.
    name: str | None
    role_ids: list[UUID]
    group_ids: list[UUID]


class DirectoryService:
    def __init__(self, profile: AccessProfile) -> None:
        self.profile = profile
        self.context = profile.context

    async def members(self) -> list[Member]:
        async with tenant_session(self.context) as session:
            rows = await session.execute(
                text(
                    # One pass with two aggregates rather than a query per user. The
                    # DISTINCTs matter: without them the two left joins multiply each
                    # other, and a user with two roles and two groups reports four of each.
                    "SELECT u.id, u.email, u.name, "
                    "  coalesce(array_agg(DISTINCT ur.role_id) "
                    "    FILTER (WHERE ur.role_id IS NOT NULL), '{}') AS role_ids, "
                    "  coalesce(array_agg(DISTINCT ug.group_id) "
                    "    FILTER (WHERE ug.group_id IS NOT NULL), '{}') AS group_ids "
                    "FROM users u "
                    "LEFT JOIN user_roles ur ON ur.user_id = u.id "
                    "LEFT JOIN user_groups ug ON ug.user_id = u.id "
                    "GROUP BY u.id, u.email, u.name "
                    "ORDER BY coalesce(u.name, u.email)"
                )
            )
            return [
                Member(
                    id=row.id,
                    email=row.email,
                    name=row.name,
                    role_ids=list(row.role_ids),
                    group_ids=list(row.group_ids),
                )
                for row in rows
            ]
