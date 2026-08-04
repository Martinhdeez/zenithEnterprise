from dataclasses import asdict

from fastapi import APIRouter

from app.features.auth.dependencies import CurrentProfile
from app.features.tenancy.schemas import TenantStatusResponse
from app.features.tenancy.status import status

router = APIRouter(tags=["tenant"])


@router.get(
    "/tenant/status",
    operation_id="getTenantStatus",
    summary="Corpus size, ingestion state and which components this installation has",
    responses={401: {"description": "Missing or invalid credentials"}},
)
async def tenant_status(profile: CurrentProfile) -> TenantStatusResponse:
    """What a client needs before it renders anything.

    Deliberately gated on authentication alone rather than on a permission. Every role can
    already discover the corpus exists by searching it, so a permission here would protect
    nothing while giving the lowest-privileged user a broken first screen.

    The counts are scoped by RLS inside the transaction, not by a filter — a number here is
    about this tenant by construction. `components` reports what is *configured*, never what
    answered a ping; liveness is what `degraded` on a real answer reports, measured on the
    request that actually needed the component.
    """
    return TenantStatusResponse(**asdict(await status(profile.context)))
