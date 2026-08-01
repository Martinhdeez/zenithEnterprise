"""Creating roles and users.

Every function here takes the session instead of opening one, so the caller decides
the transaction. That matters because the same operations run from two very different
places: the install CLI, on the owner connection, before any context exists (mvp.md
2.4), and the admin API in M3, on a tenant session under RLS. Neither needs its own
copy, and neither gets to widen the other's reach.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.features.auth.model import Role, RolePermission, User, UserRole
from app.features.auth.permissions import SYSTEM_ROLES
from app.features.auth.service import normalise_email
from app.features.auth.throttle import run_hash


async def seed_system_roles(session: AsyncSession, tenant_id: UUID) -> dict[str, Role]:
    """Create `admin` and `member` for a new tenant.

    Marked `is_system` so they cannot be deleted: without this, an administrator can
    remove the last role holding administration permissions and lock the tenant out of
    itself. On-premise, recovering from that means someone in `psql`.

    `admin` gets the whole catalogue, which is why it is read from `SYSTEM_ROLES`
    rather than listed again — a permission added to the catalogue and forgotten here
    would be a permission no one in a fresh installation can ever hold.
    """
    created: dict[str, Role] = {}
    for name, codes in SYSTEM_ROLES.items():
        role = Role(tenant_id=tenant_id, name=name, is_system=True)
        session.add(role)
        await session.flush()
        session.add_all(
            RolePermission(role_id=role.id, permission_code=code) for code in sorted(codes)
        )
        created[name] = role
    await session.flush()
    return created


async def create_user(
    session: AsyncSession,
    tenant_id: UUID,
    email: str,
    password: str,
    role_ids: list[UUID] | None = None,
) -> User:
    """Create a user and give them their roles.

    The password arrives already chosen and is hashed here, never stored or logged in
    the clear. The CLI generates it and prints it once rather than taking it as an
    argument, because an argument ends up in the shell history and the process table.
    """
    user = User(
        tenant_id=tenant_id,
        email=normalise_email(email),
        # Off the event loop for the same reason login is: argon2 blocks for as long as
        # it runs, and an invitation must not stall every other request in the process.
        password_hash=await run_hash(lambda: hash_password(password)),
    )
    session.add(user)
    await session.flush()
    session.add_all(UserRole(user_id=user.id, role_id=role_id) for role_id in role_ids or [])
    await session.flush()
    return user
