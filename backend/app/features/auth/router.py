from dataclasses import asdict

from fastapi import APIRouter, Depends, Request, status

from app.common.exceptions import RateLimitedError
from app.features.auth.dependencies import CurrentProfile
from app.features.auth.schemas import (
    LoginRequest,
    MeResponse,
    PasswordChange,
    ProfileResponse,
    RefreshRequest,
    TokenResponse,
)
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


@router.get("/profile")
async def profile(current: CurrentProfile) -> ProfileResponse:
    """Everything the profile screen shows, in one request.

    Separate from `/me`, which is the machine-readable version a client uses to decide
    what to render and is on the path of every session. This one carries names and counts
    and is read when somebody opens a screen.
    """
    described = await AuthService().describe(current)
    return ProfileResponse(**asdict(described))


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(request: PasswordChange, current: CurrentProfile) -> None:
    """Change your own password.

    Until this existed a password was generated at invitation and could only be replaced
    by an administrator with shell access. Every other session is signed out as a
    consequence — see `AuthService.change_password`.
    """
    await AuthService().change_password(current, request.current_password, request.new_password)


@router.post("/sign-out-everywhere", status_code=status.HTTP_204_NO_CONTENT)
async def sign_out_everywhere(current: CurrentProfile) -> None:
    """Invalidate every token for this user, this one included.

    The caller is signed out too, and that is the point rather than a side effect: "sign
    out everywhere" that spared the device asking would leave the one session an attacker
    is most likely to be holding.
    """
    await AuthService().sign_out_everywhere(current)


@router.get("/me")
async def me(profile: CurrentProfile) -> MeResponse:
    return MeResponse(
        user_id=profile.user_id,
        tenant_id=profile.context.tenant_id,
        permissions=sorted(profile.permissions),
        label_ids=list(profile.context.label_ids),
    )
