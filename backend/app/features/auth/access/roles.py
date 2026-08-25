"""Role management (mvp.md 2.1), and the one operation that must be refused.

Roles are how a tenant decides who may do what. Everything here runs inside
`tenant_session`, so a role belonging to another customer is not forbidden — it is absent,
and a request naming it is a 404 rather than a 403, because the difference between them
confirms that it exists.

**The refusal that matters**: `ADMINISTRATION` is the pair of permissions that, if nobody
holds them, locks a tenant out of its own administration. Recovering from that on-premise
means somebody in `psql` on a customer's server. So an edit that would leave the tenant
with no administrator is rejected — not warned about, rejected — and that check is the
reason this module exists rather than the routes being three lines of CRUD.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

from app.common.exceptions import ConflictError, NotFoundError
from app.core.database import tenant_session
from app.features.auth.access.permissions import ADMINISTRATION, CATALOGUE
from app.features.auth.service import AccessProfile

MANAGE = "roles.manage"


@dataclass(frozen=True, slots=True)
class Role:
    id: UUID
    name: str
    is_system: bool
    permissions: list[str]
    #: How many users hold it. A role about to be edited is safer to reason about when you
    #: know whether it is in use.
    users: int
    #: Clearance, 1 to 10. Reaches every label whose own level is at or below it.
    priority_level: int
    #: Labels granted to this role outright, which is the only route to a compartment.
    #: Separate from clearance on purpose: these are the ones an administrator chose.
    label_ids: list[UUID]


class RoleService:
    def __init__(self, profile: AccessProfile) -> None:
        self.profile = profile
        self.context = profile.context

    async def visible(self) -> list[Role]:
        """Named `visible` rather than `list`, and not for style.

        A method called `list` shadows the builtin inside the class body, where annotations
        are evaluated — so `permissions: list[str]` on the next method resolves to this
        function and the module fails to import. It is also the more accurate name: RLS
        decides what this returns.
        """
        async with tenant_session(self.context) as session:
            rows = await session.execute(
                text(
                    "SELECT r.id, r.name, r.is_system, r.priority_level, "
                    "  coalesce(array_agg(DISTINCT rp.permission_code) "
                    "    FILTER (WHERE rp.permission_code IS NOT NULL), '{}') AS permissions, "
                    "  coalesce(array_agg(DISTINCT rl.label_id) "
                    "    FILTER (WHERE rl.label_id IS NOT NULL), '{}') AS label_ids, "
                    "  (SELECT count(*) FROM user_roles ur WHERE ur.role_id = r.id) AS users "
                    "FROM roles r "
                    "LEFT JOIN role_permissions rp ON rp.role_id = r.id "
                    "LEFT JOIN role_labels rl ON rl.role_id = r.id "
                    "GROUP BY r.id, r.name, r.is_system, r.priority_level "
                    "ORDER BY r.priority_level DESC, r.is_system DESC, r.name"
                )
            )
            return [
                Role(
                    id=row.id,
                    name=row.name,
                    is_system=row.is_system,
                    permissions=sorted(row.permissions),
                    users=int(row.users),
                    priority_level=int(row.priority_level),
                    label_ids=list(row.label_ids),
                )
                for row in rows
            ]

    async def create(self, name: str, permissions: list[str], priority_level: int = 1) -> Role:
        self._known(permissions)
        async with tenant_session(self.context) as session:
            role_id = await session.scalar(
                text(
                    "INSERT INTO roles (tenant_id, name, is_system, priority_level) "
                    "VALUES (:t, :name, false, :p) RETURNING id"
                ),
                {"t": self.context.tenant_id, "name": name, "p": priority_level},
            )
            await self._write_permissions(session, UUID(str(role_id)), permissions)
        return next(role for role in await self.visible() if role.id == UUID(str(role_id)))

    async def set_clearance(self, role_id: UUID, priority_level: int) -> Role:
        """How much this role's holders may reach vertically.

        Allowed on system roles, unlike permissions. Stripping `admin` of `roles.manage`
        can lock a tenant out of its own administration and is refused for that reason;
        changing what an administrator may *read* locks nobody out of anything, and a
        customer who wants their admin role to carry no clearance is entitled to that.
        """
        existing = await self._must_exist(role_id)
        if existing.priority_level == priority_level:
            return existing
        async with tenant_session(self.context) as session:
            await session.execute(
                text("UPDATE roles SET priority_level = :p WHERE id = :r"),
                {"p": priority_level, "r": role_id},
            )
        return await self._one(role_id)

    async def set_labels(self, role_id: UUID, label_ids: list[UUID]) -> Role:
        """Replace the labels granted to this role outright.

        A grant is unconditional — it ignores groups and ignores clearance — so this is the
        route that opens a compartment, and the only one. Replace rather than patch, for
        the same reason `set_permissions` does.
        """
        await self._must_exist(role_id)
        async with tenant_session(self.context) as session:
            known = set(
                await session.scalars(
                    text("SELECT id FROM access_labels WHERE id = ANY(:ids)"),
                    {"ids": [str(label_id) for label_id in label_ids]},
                )
            )
            unknown = [str(i) for i in label_ids if i not in known]
            if unknown:
                raise NotFoundError(f"no label(s): {', '.join(unknown)}")

            await session.execute(
                text("DELETE FROM role_labels WHERE role_id = :r"), {"r": role_id}
            )
            for label_id in sorted(set(label_ids), key=str):
                await session.execute(
                    text(
                        "INSERT INTO role_labels (role_id, label_id) VALUES (:r, :l) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"r": role_id, "l": label_id},
                )
        return await self._one(role_id)

    async def delete(self, role_id: UUID) -> None:
        """Delete a role, unless it is a system role or the tenant's last administrator."""
        existing = await self._must_exist(role_id)
        if existing.is_system:
            raise ConflictError("system roles cannot be deleted")

        remaining = {role.id: role for role in await self.visible() if role.id != role_id}
        held: set[str] = set()
        for role in remaining.values():
            if role.users:
                held |= set(role.permissions)
        if not held >= ADMINISTRATION:
            missing = ", ".join(sorted(ADMINISTRATION - held))
            raise ConflictError(
                f"this would leave nobody in the tenant holding: {missing}. "
                f"Grant them to another role first."
            )

        async with tenant_session(self.context) as session:
            await session.execute(text("DELETE FROM roles WHERE id = :r"), {"r": role_id})

    async def _must_exist(self, role_id: UUID) -> Role:
        roles = {role.id: role for role in await self.visible()}
        if role_id not in roles:
            raise NotFoundError(f"no role {role_id}")
        return roles[role_id]

    async def _one(self, role_id: UUID) -> Role:
        return next(role for role in await self.visible() if role.id == role_id)

    async def set_permissions(self, role_id: UUID, permissions: list[str]) -> Role:
        """Replace a role's permissions, unless doing so orphans the tenant.

        Replace rather than patch: a caller sending the full set knows what the role will
        hold afterwards, while an add/remove API makes the result depend on state they
        did not read.
        """
        self._known(permissions)
        existing = {role.id: role for role in await self.visible()}
        if role_id not in existing:
            raise NotFoundError(f"no role {role_id}")

        if existing[role_id].is_system:
            # System roles are seeded per tenant and referenced by provisioning. Letting a
            # customer strip `admin` of `roles.manage` is the lockout this module refuses,
            # by a longer route.
            raise ConflictError("system roles cannot be edited")

        self._keeps_an_administrator(existing, role_id, permissions)

        async with tenant_session(self.context) as session:
            await session.execute(
                text("DELETE FROM role_permissions WHERE role_id = :r"), {"r": role_id}
            )
            await self._write_permissions(session, role_id, permissions)
        return next(role for role in await self.visible() if role.id == role_id)

    async def assign(self, user_id: UUID, role_ids: list[UUID]) -> None:
        """Replace a user's roles.

        The user is reached through RLS, so assigning a role to somebody in another tenant
        is not a permission error — there is no such user from here.
        """
        async with tenant_session(self.context) as session:
            exists = await session.scalar(text("SELECT 1 FROM users WHERE id = :u"), {"u": user_id})
            if not exists:
                raise NotFoundError(f"no user {user_id}")

            known = {role.id for role in await self.visible()}
            unknown = [str(role_id) for role_id in role_ids if role_id not in known]
            if unknown:
                raise NotFoundError(f"no role(s): {', '.join(unknown)}")

            await session.execute(text("DELETE FROM user_roles WHERE user_id = :u"), {"u": user_id})
            for role_id in role_ids:
                await session.execute(
                    text("INSERT INTO user_roles (user_id, role_id) VALUES (:u, :r)"),
                    {"u": user_id, "r": role_id},
                )

    def _known(self, permissions: list[str]) -> None:
        """Every permission must be one the software knows how to enforce.

        A permission nobody checks is a lie in the administration screen: it appears
        granted, and grants nothing.
        """
        unknown = sorted(set(permissions) - set(CATALOGUE))
        if unknown:
            raise NotFoundError(f"unknown permission(s): {', '.join(unknown)}")

    def _keeps_an_administrator(
        self, existing: dict[UUID, Role], role_id: UUID, permissions: list[str]
    ) -> None:
        """Refuse an edit that would leave nobody able to administer the tenant.

        Checked across *held* roles rather than all roles: a role with the permissions and
        no users protects nobody. Recovering from a lockout on-premise means somebody in
        `psql` on the customer's server, which is not a support call this product should
        ever generate.
        """
        remaining: set[str] = set()
        for other_id, role in existing.items():
            held = role.permissions if other_id != role_id else permissions
            if role.users or other_id == role_id:
                remaining |= set(held)

        if not remaining >= ADMINISTRATION:
            missing = ", ".join(sorted(ADMINISTRATION - remaining))
            raise ConflictError(
                f"this would leave nobody in the tenant holding: {missing}. "
                f"Grant them to another role first."
            )

    async def _write_permissions(self, session: object, role_id: UUID, codes: list[str]) -> None:
        for code in sorted(set(codes)):
            await session.execute(  # type: ignore[attr-defined]
                text(
                    "INSERT INTO role_permissions (role_id, permission_code) "
                    "VALUES (:r, :c) ON CONFLICT DO NOTHING"
                ),
                {"r": role_id, "c": code},
            )
