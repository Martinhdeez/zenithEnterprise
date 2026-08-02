"""The label every new tenant starts with.

F3 built the machinery for a default label and left the seeding to whoever needed it
first. F4 is that caller: an upload with no label specified has to be classified as
*something*, and the alternative — storing it with an empty array — means visible to the
whole tenant, arrived at by omission rather than by decision.

Granting it to both system roles is the part that is easy to leave out and impossible to
notice. A default label no role reaches would make every unclassified upload invisible to
everyone, including the administrator who uploaded it, and the product would look broken
in a way no error message explains.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.features.auth.model import Role
from app.features.labels.model import AccessLabel, RoleLabel

DEFAULT_LABEL = "General"


async def seed_default_label(
    session: AsyncSession, tenant_id: UUID, roles: list[Role]
) -> AccessLabel:
    label = AccessLabel(tenant_id=tenant_id, name=DEFAULT_LABEL, is_default=True)
    session.add(label)
    await session.flush()
    # Only the system roles. A role created later reaches `General` when an administrator
    # says so, which keeps a new compartment narrow by default rather than wide.
    session.add_all(RoleLabel(role_id=role.id, label_id=label.id) for role in roles)
    await session.flush()
    return label
