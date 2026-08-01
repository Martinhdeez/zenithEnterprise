"""The chain from a bearer token to a permission decision.

    Authorization: Bearer <token>
            -> current_profile   identity, permissions, reachable labels
            -> requires(...)     403, or the handler runs

`requires` is a dependency rather than a call inside the handler, and that is the whole
point. A check written in the body is invisible until someone reads the body, and it is
forgotten in exactly the endpoint nobody reviewed. A dependency is in the signature and
in the OpenAPI schema, so an unprotected endpoint looks unprotected.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.common.exceptions import AuthenticationError, PermissionDeniedError
from app.features.auth.service import AccessProfile, AuthService

# `auto_error=False` so a missing header raises our own 401 through the domain error
# handler, rather than FastAPI's, keeping every authentication failure one shape.
_bearer = HTTPBearer(auto_error=False)


async def current_profile(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AccessProfile:
    if credentials is None:
        raise AuthenticationError("missing bearer token")
    user_id, tenant_id = AuthService().principal(credentials.credentials)
    return await AuthService().profile(user_id, tenant_id)


CurrentProfile = Annotated[AccessProfile, Depends(current_profile)]


def requires(permission: str) -> Callable[[AccessProfile], Awaitable[AccessProfile]]:
    """Declare the permission an endpoint needs.

        @router.post("/documents", dependencies=[Depends(requires("documents.upload"))])

    The code is compared against what the database says the caller's roles grant, never
    against an enum. That is what makes a custom role configuration rather than a
    release (mvp.md 2.1).
    """

    async def guard(profile: CurrentProfile) -> AccessProfile:
        if permission not in profile.permissions:
            raise PermissionDeniedError(f"this action requires {permission!r}")
        return profile

    return guard
