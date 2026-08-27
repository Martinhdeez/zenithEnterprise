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

#: Where an upload that named no compartment waits for the classifier. See migration 0017.
QUARANTINE_LABEL = "Unclassified"


async def seed_quarantine_label(
    session: AsyncSession, tenant_id: UUID, roles: list[Role]
) -> AccessLabel:
    """The label an unfiled upload lands in, granted to `admin` and to nobody else.

    The narrowness is the whole feature. Granted to both system roles it would reach exactly
    as far as the default label it replaces, which would be an elaborate way of changing
    nothing — and the point is that a document nobody has classified must not be readable by
    everybody while it ingests.

    A `member` is not locked out of their own upload: migration 0017's policy lets an
    uploader read their own document while its status is not `ready`. That exception expires
    with ingestion, so once the classifier has filed it, labels answer and nothing else does.
    """
    label = AccessLabel(tenant_id=tenant_id, name=QUARANTINE_LABEL, is_quarantine=True)
    session.add(label)
    await session.flush()
    session.add_all(
        RoleLabel(role_id=role.id, label_id=label.id) for role in roles if role.name == "admin"
    )
    await session.flush()
    return label


async def ensure_quarantine_label(session: AsyncSession, tenant_id: UUID) -> AccessLabel:
    """The tenant's quarantine label, restored if somebody removed it.

    The same repair, and the same reasoning, as `ensure_default_label` — including why
    repairing beats refusing. The failure here is worse than the default label's, though:
    without this label an upload has nowhere narrow to land, and the only alternatives are
    refusing the upload or putting it back in the tenant-wide default. Recreating what
    provisioning would have made is the one answer that is neither.
    """
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    existing = await session.scalar(
        select(AccessLabel).where(AccessLabel.tenant_id == tenant_id, AccessLabel.is_quarantine)
    )
    if existing is not None:
        return existing

    roles = list(
        await session.scalars(select(Role).where(Role.tenant_id == tenant_id, Role.is_system))
    )
    try:
        async with session.begin_nested():
            return await seed_quarantine_label(session, tenant_id, roles)
    except IntegrityError:
        # Lost the race against a concurrent upload. Read theirs.
        restored = await session.scalar(
            select(AccessLabel).where(AccessLabel.tenant_id == tenant_id, AccessLabel.is_quarantine)
        )
        assert restored is not None, "the constraint fired, so a quarantine label exists"
        return restored


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


async def ensure_default_label(session: AsyncSession, tenant_id: UUID) -> AccessLabel:
    """The tenant's default label, restored if somebody removed it.

    A tenant is created with one and there is no supported way to end up without it — and a
    tenant was found without it anyway, which is what this exists for. The symptom is total:
    `DocumentService` refuses every upload that names no label, because storing one with an
    empty array publishes it to the whole tenant by omission. So a deleted label locks the
    product's most common action, and the error is about a concept nobody outside this
    codebase has heard of.

    Repairing rather than refusing is a judgement, and it is worth being explicit about which
    way it cuts. This creates a label reachable by the tenant's *system* roles — exactly what
    provisioning would have created, no wider — so nothing becomes visible to anybody who
    would not have seen an unclassified upload the day the tenant was made. It is not a
    licence for other code to invent labels: the classifier still cannot, and must not.

    Safe under concurrency without a lock. `uq_access_labels_one_default_per_tenant` is a
    partial unique index, so two uploads racing here means one insert wins and the other
    raises — and re-reading is the correct response to "somebody else already fixed it".
    """
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    existing = await session.scalar(
        select(AccessLabel).where(AccessLabel.tenant_id == tenant_id, AccessLabel.is_default)
    )
    if existing is not None:
        return existing

    # System roles only, which is what `seed_default_label` is given at provisioning. A
    # role an administrator created later reaches `General` when they say so — restoring a
    # label should not quietly hand it to compartments that never had it.
    roles = list(
        await session.scalars(select(Role).where(Role.tenant_id == tenant_id, Role.is_system))
    )
    try:
        async with session.begin_nested():
            return await seed_default_label(session, tenant_id, roles)
    except IntegrityError:
        # Lost the race. The other transaction created it; read theirs.
        restored = await session.scalar(
            select(AccessLabel).where(AccessLabel.tenant_id == tenant_id, AccessLabel.is_default)
        )
        assert restored is not None, "the constraint fired, so a default exists"
        return restored
