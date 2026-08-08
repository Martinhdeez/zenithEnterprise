"""Administration above every tenant.

The only surface in the product that crosses the tenant boundary, so it is also the only one
where the usual defence — RLS, which makes another customer's rows invisible rather than
forbidden — is switched off. What replaces it is `requires_system_admin` on every route
below, and there is nothing else. That is why the dependency is declared once per route
rather than assumed from the prefix: a route added later without it would be a route with no
protection at all, and this way the omission is visible on the line it happens.

Mounted at `/system`, not `/api/v1/system`: no route in this API carries a version prefix,
and introducing one here would make this the only corner of the surface that does.
"""

from dataclasses import asdict
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.features.auth.dependencies import requires_system_admin
from app.features.system.schemas import (
    OrganisationResponse,
    ProvisionRequest,
    ProvisionResponse,
    PurgeRequest,
)
from app.features.system.service import SystemService

router = APIRouter(tags=["system"], dependencies=[Depends(requires_system_admin)])


@router.get(
    "/system/tenants",
    operation_id="listOrganisations",
    summary="Every organisation in the installation",
)
async def list_organisations() -> list[OrganisationResponse]:
    return [OrganisationResponse(**asdict(org)) for org in await SystemService().organisations()]


@router.post(
    "/system/tenants",
    operation_id="provisionOrganisation",
    status_code=status.HTTP_201_CREATED,
    summary="Create an organisation and its first administrator",
    responses={409: {"description": "An organisation with that name already exists"}},
)
async def provision(request: ProvisionRequest) -> ProvisionResponse:
    """The password comes back once and is never recoverable.

    Same contract as inviting a colleague, and for the same reason: this product ships into
    networks with no outbound mail, so requiring a mail server to create a customer would
    make the feature undeployable exactly where the product is sold.
    """
    result = await SystemService().provision(request.name, request.admin_email)
    return ProvisionResponse(
        organisation=OrganisationResponse(**asdict(result.organisation)),
        admin_email=result.admin_email,
        password=result.password,
    )


@router.post(
    "/system/tenants/{tenant_id}/suspend",
    operation_id="suspendOrganisation",
    summary="Cut off access without touching any data",
    responses={
        404: {"description": "No such organisation"},
        409: {"description": "An organisation being purged cannot be suspended"},
    },
)
async def suspend(tenant_id: UUID) -> OrganisationResponse:
    """Takes effect on the caller's very next request, not when their token expires.

    The check lives in `AuthService.profile`, which every authenticated route resolves, so
    an active browser session stops working immediately rather than in fifteen minutes.
    """
    return OrganisationResponse(**asdict(await SystemService().suspend(tenant_id)))


@router.post(
    "/system/tenants/{tenant_id}/activate",
    operation_id="activateOrganisation",
    summary="Restore a suspended organisation",
    responses={
        404: {"description": "No such organisation"},
        409: {"description": "A purged organisation cannot be restored"},
    },
)
async def activate(tenant_id: UUID) -> OrganisationResponse:
    return OrganisationResponse(**asdict(await SystemService().activate(tenant_id)))


@router.post(
    "/system/tenants/{tenant_id}/purge",
    operation_id="purgeOrganisation",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Destroy an organisation's data. Irreversible.",
    responses={
        404: {"description": "No such organisation"},
        409: {"description": "Not suspended first, or the name does not match"},
    },
)
async def purge(tenant_id: UUID, request: PurgeRequest) -> OrganisationResponse:
    """202, not 200: the rows and files are still going when this answers.

    Two brakes, both re-checked here rather than trusted to the screen — the organisation
    must already be suspended, and the caller must repeat its name. A wrong build of the
    front end must not be able to destroy a customer on its own.
    """
    organisation = await SystemService().begin_purge(tenant_id, request.confirm_name)

    from app.features.ingestion.tasks import app as queue
    from app.features.ingestion.tasks import purge_tenant

    async with queue.open_async():
        await purge_tenant.defer_async(tenant_id=str(tenant_id))

    return OrganisationResponse(**asdict(organisation))
