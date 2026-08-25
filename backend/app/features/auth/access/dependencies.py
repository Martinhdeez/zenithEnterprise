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
from app.core.request_context import bind_tenant
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
    # Here rather than in the middleware, which runs before anything has read the token — a
    # tenant guessed from an unverified header would be worse than no tenant at all. The user
    # id is deliberately not bound: `request_context` explains why it belongs in the audit
    # trail rather than in an operational log a support engineer tails.
    bind_tenant(tenant_id)
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


def requires_any(*permissions: str) -> Callable[[AccessProfile], Awaitable[AccessProfile]]:
    """Declare that any one of several permissions opens the endpoint.

    For the pairs that differ in scope rather than in kind — `documents.delete.own` and
    `documents.delete.any` both permit deletion, and which document is a question the
    service answers once it can see the row. Splitting them into two endpoints would put
    the same operation at two URLs and let a client discover ownership by trying both.
    """

    async def guard(profile: CurrentProfile) -> AccessProfile:
        if profile.permissions.isdisjoint(permissions):
            raise PermissionDeniedError(
                f"this action requires one of: {', '.join(sorted(permissions))}"
            )
        return profile

    return guard


async def requires_system_admin(profile: CurrentProfile) -> AccessProfile:
    """Declare that an endpoint is above every tenant.

        @router.get("/system/tenants", dependencies=[Depends(requires_system_admin)])

    Not a permission, and it cannot be one. `requires("...")` compares against what the
    caller's roles grant, and a tenant's own administrator edits those roles freely from the
    roles screen — so any code in `CATALOGUE` is a route out of your own tenant, held open by
    the very people it is meant to bound. This reads `users.is_system_admin`, which migration
    0010 made unwritable on the application connection.

    A tenant administrator holding the entire catalogue gets 403 here. There is a test that
    says exactly that, because it is the property the whole panel rests on.
    """
    if not profile.is_system_admin:
        raise PermissionDeniedError("this action requires system administration")
    return profile
