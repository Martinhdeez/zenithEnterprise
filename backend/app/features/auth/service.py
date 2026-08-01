import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import jwt
import structlog
from sqlalchemy import text

from app.common.exceptions import AuthenticationError
from app.core.database import tenant_session, unscoped_session
from app.core.security import (
    TokenKind,
    decode_token,
    hash_password,
    issue_token,
    verify_password,
)
from app.features.auth.repository import UserRepository
from app.features.auth.throttle import run_hash
from app.features.tenancy.context import TenantContext

log = structlog.get_logger(__name__)

# Hashed once at import so that verification against a non-existent account costs the
# same as verification against a real one. Skipping the hash when the user is unknown
# makes response time an account-enumeration oracle: a fast 401 means "no such user",
# a slow 401 means "wrong password".
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


def normalise_email(email: str) -> str:
    """Addresses are stored and compared lowercased.

    Local parts are technically case-sensitive, but no one treats them that way, and a
    user who signs up as `Ana@x.com` and later types `ana@x.com` must not be told their
    password is wrong.
    """
    return email.strip().lower()


@dataclass(frozen=True, slots=True)
class Credentials:
    user_id: UUID
    tenant_id: UUID
    password_hash: str
    token_version: int


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str


@dataclass(frozen=True, slots=True)
class AccessProfile:
    """Everything a request needs to decide what the caller may do.

    Resolved once per request. `context` is what gets pinned into the transaction, so
    it is the value the database will trust completely.
    """

    user_id: UUID
    context: TenantContext
    permissions: frozenset[str]


class AuthService:
    async def authenticate(self, email: str, password: str) -> TokenPair:
        candidates = await self._lookup(normalise_email(email))

        if len(candidates) == 1:
            found = candidates[0]
        else:
            # Either no such address, or the same address in more than one tenant of a
            # hosted install. The second case needs a tenant on the request to resolve,
            # which the MVP does not have, and saying so would confirm the address
            # exists. It is logged instead, so an operator can see it.
            if candidates:
                log.warning("login_ambiguous_email", tenants=len(candidates))
            found = None

        # Runs in both branches, on purpose. See `_DUMMY_HASH`. Off the event loop and
        # under a concurrency cap, because argon2 is expensive by design and this path
        # is reachable without credentials — see `throttle.py`.
        stored = found.password_hash if found else _DUMMY_HASH
        valid = await run_hash(lambda: verify_password(password, stored))
        if found is None or not valid:
            raise AuthenticationError("invalid email or password")

        return self._issue(found.user_id, found.tenant_id, found.token_version)

    async def refresh(self, refresh_token: str) -> TokenPair:
        """Exchange a refresh token for a new pair.

        This is the only authentication path that reads the database, which is what
        makes `token_version` effective without a per-request query: a bumped version
        cannot survive the next refresh, so revocation takes at most one access-token
        lifetime.
        """
        payload = self._decode(refresh_token, "refresh")
        user_id = UUID(payload["sub"])
        tenant_id = UUID(payload["tid"])

        async with tenant_session(TenantContext(tenant_id=tenant_id)) as session:
            user = await UserRepository(session).get(user_id)
            if user is None or user.token_version != payload["ver"]:
                raise AuthenticationError("the session is no longer valid")
            return self._issue(user.id, user.tenant_id, user.token_version)

    async def profile(self, user_id: UUID, tenant_id: UUID) -> AccessProfile:
        """Resolve permissions and reachable labels for the request.

        The tenant comes from the token; the labels come from the roles. This is the
        single place labels are resolved, and it is deliberately not somewhere a
        handler can influence.
        """
        async with tenant_session(TenantContext(tenant_id=tenant_id)) as session:
            users = UserRepository(session)
            permissions = await users.permission_codes(user_id)
            labels = await users.label_ids(user_id)

        return AccessProfile(
            user_id=user_id,
            context=TenantContext.for_tenant(tenant_id, labels),
            permissions=permissions,
        )

    def principal(self, access_token: str) -> tuple[UUID, UUID]:
        """Identity from an access token, with no database access at all.

        Access tokens are stateless by design (mvp.md 2.4): checking them against a
        table would put a query in front of every request to buy revocation latency
        that the refresh path already provides.
        """
        payload = self._decode(access_token, "access")
        return UUID(payload["sub"]), UUID(payload["tid"])

    async def _lookup(self, email: str) -> list[Credentials]:
        """Find an account before any context exists.

        Goes through `zenith_authenticate_lookup`, a `SECURITY DEFINER` function that
        can return four fields and nothing else. The alternative — an owner session in
        a request handler — would put a connection that reads every row in the
        installation on the unauthenticated path.
        """
        async with unscoped_session() as session:
            rows = await session.execute(
                text(
                    "SELECT user_id, tenant_id, password_hash, token_version "
                    "FROM zenith_authenticate_lookup(:email)"
                ),
                {"email": email},
            )
            return [Credentials(*row) for row in rows]

    def _issue(self, user_id: UUID, tenant_id: UUID, token_version: int) -> TokenPair:
        return TokenPair(
            access_token=issue_token("access", user_id, tenant_id, token_version),
            refresh_token=issue_token("refresh", user_id, tenant_id, token_version),
        )

    def _decode(self, token: str, kind: TokenKind) -> dict[str, Any]:
        try:
            return decode_token(token, kind)
        except jwt.PyJWTError as exc:
            # Expired, tampered with, or the wrong kind: all one answer to the caller.
            raise AuthenticationError("invalid or expired token") from exc
