from uuid import uuid4

import pytest

from app.common.exceptions import ConflictError, NotFoundError
from app.features.tenancy.service import TenantService

pytestmark = pytest.mark.asyncio


async def test_create_persists_the_tenant(configured_engines: None) -> None:
    name = f"Acme {uuid4()}"

    created = await TenantService().create(name)
    found = await TenantService().by_name(name)

    assert created.id == found.id
    assert found.name == name


async def test_duplicate_name_raises_a_domain_error(configured_engines: None) -> None:
    """The UNIQUE constraint has to surface as something the CLI can print, not as a
    driver traceback in front of whoever is installing the product."""
    name = f"Duplicate {uuid4()}"
    await TenantService().create(name)

    with pytest.raises(ConflictError):
        await TenantService().create(name)


async def test_unknown_name_raises_not_found(configured_engines: None) -> None:
    with pytest.raises(NotFoundError):
        await TenantService().by_name(f"missing {uuid4()}")


async def test_context_for_starts_with_no_labels(configured_engines: None) -> None:
    """The narrow default. Labels come from roles, which do not exist until F2;
    defaulting to "everything" would make the first caller that forgets label
    resolution see the whole tenant."""
    tenant = await TenantService().create(f"Narrow {uuid4()}")

    context = await TenantService().context_for(tenant.id)

    assert context.tenant_id == tenant.id
    assert context.label_ids == ()
