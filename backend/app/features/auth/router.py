from fastapi import APIRouter, Depends, Request

from app.common.exceptions import RateLimitedError
from app.features.auth.dependencies import CurrentProfile
from app.features.auth.schemas import LoginRequest, MeResponse, RefreshRequest, TokenResponse
from app.features.auth.service import AuthService
from app.features.auth.throttle import login_limiter

router = APIRouter(prefix="/auth", tags=["auth"])


async def throttle_login(request: Request) -> None:
    """Cap login attempts per source address.

    Keyed on the peer address, which behind a reverse proxy is the proxy unless it is
    run with `--proxy-headers` and a trusted forwarded-for list. Getting that wrong
    turns this into a single shared bucket for the whole installation, so it belongs in
    the deployment checklist rather than in a comment nobody reads.
    """
    client = request.client.host if request.client else "unknown"
    if not login_limiter.allow(client):
        raise RateLimitedError("too many login attempts, try again in a minute")


@router.post("/login", dependencies=[Depends(throttle_login)])
async def login(request: LoginRequest) -> TokenResponse:
    pair = await AuthService().authenticate(request.email, request.password)
    return TokenResponse(access_token=pair.access_token, refresh_token=pair.refresh_token)


@router.post("/refresh")
async def refresh(request: RefreshRequest) -> TokenResponse:
    pair = await AuthService().refresh(request.refresh_token)
    return TokenResponse(access_token=pair.access_token, refresh_token=pair.refresh_token)


@router.get("/me")
async def me(profile: CurrentProfile) -> MeResponse:
    return MeResponse(
        user_id=profile.user_id,
        tenant_id=profile.context.tenant_id,
        permissions=sorted(profile.permissions),
        label_ids=list(profile.context.label_ids),
    )
