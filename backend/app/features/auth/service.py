import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import jwt
import structlog
from sqlalchemy import text

from app.common.exceptions import AuthenticationError, NotFoundError, TenantSuspendedError
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
from app.features.tenancy.model import ACTIVE

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
class Profile:
    """Who the caller is, in terms a person recognises.

    Deliberately separate from `AccessProfile`, which is what a *request* is authorised
    with and is resolved on every single one. This is read once, by a screen, and carries
    names and counts that would be wasted work on the path to every document listing.
    """

    user_id: UUID
    email: str
    name: str | None
    tenant_id: UUID
    tenant_name: str | None
    roles: list[str]
    permissions: list[str]
    #: Names, not ids. The answer to "why can I not see my colleague's document".
    labels: list[str]
    documents_uploaded: int
    created_at: datetime
    #: Authority above every tenant. The nav item for the system panel hangs off this, and
    #: the API refuses those routes regardless — hiding it only spares somebody a screen
    #: full of 403s.
    is_system_admin: bool = False


@dataclass(frozen=True, slots=True)
class AccessProfile:
    """Everything a request needs to decide what the caller may do.

    Resolved once per request. `context` is what gets pinned into the transaction, so
    it is the value the database will trust completely.
    """

    user_id: UUID
    context: TenantContext
    permissions: frozenset[str]
    #: Authority above every tenant. Read from `users` per request rather than carried in
    #: the token, so revoking it takes effect on the next request instead of whenever the
    #: access token happens to expire.
    is_system_admin: bool = False
    #: The caller's own address, stamped onto audit rows so the record survives their
    #: deletion. Resolved in the same session that reads permissions, so it is free.
    email: str = ""


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

        **It is also where a suspended organisation is cut off**, and it has to be here
        rather than at login. Refusing to issue new tokens would leave everybody already
        signed in working for up to an access token's lifetime, and "suspend this customer"
        that takes fifteen minutes to mean anything is not a suspension. This runs on every
        request, so the next one after the switch is thrown is refused.

        It costs no extra round trip: the tenant's own row is visible under its own RLS
        policy inside the session this method already opens.
        """
        async with tenant_session(TenantContext(tenant_id=tenant_id)) as session:
            users = UserRepository(session)
            permissions = await users.permission_codes(user_id)
            labels = await users.label_ids(user_id)
            status = await users.tenant_status()
            is_system_admin = await users.is_system_admin(user_id)
            email = await users.email(user_id)

        # System administrators are exempt, and must be: the panel that reactivates a
        # suspended organisation is reachable from an account that lives in one, and a
        # check without this exception can lock the operator out of their own recovery.
        # `/system/*` is the only surface they reach while suspended — every other route
        # resolves this same profile and refuses below.
        if status != ACTIVE and not is_system_admin:
            raise TenantSuspendedError("this organisation is suspended")

        return AccessProfile(
            user_id=user_id,
            context=TenantContext.for_tenant(tenant_id, labels),
            permissions=permissions,
            is_system_admin=is_system_admin,
            email=email,
        )

    async def describe(self, profile: AccessProfile) -> Profile:
        """The profile screen's one request.

        Runs inside the caller's own context, so every name here is one they already
        reach — a label they cannot use is not in `context.label_ids` and so is never
        looked up, and the tenant read is scoped by RLS to their own.
        """
        async with tenant_session(profile.context) as session:
            users = UserRepository(session)
            user = await users.get(profile.user_id)
            if user is None:
                # Deleted between issuing the token and using it. The token is still
                # cryptographically valid, which is exactly the gap `token_version` and a
                # short expiry exist to bound; there is nothing to describe.
                raise NotFoundError("no such user")
            return Profile(
                user_id=user.id,
                email=user.email,
                name=user.name,
                tenant_id=profile.context.tenant_id,
                tenant_name=await users.tenant_name(),
                roles=await users.role_names(user.id),
                permissions=sorted(profile.permissions),
                is_system_admin=profile.is_system_admin,
                labels=await users.label_names(profile.context.label_ids),
                documents_uploaded=await users.documents_uploaded(user.id),
                created_at=user.created_at,
            )

    async def rename(self, profile: AccessProfile, name: str) -> Profile:
        """Set or clear your own display name.

        Whitespace-only is stored as null rather than as a string of spaces, so "not set"
        has one representation and every fallback to the email keeps working.
        """
        async with tenant_session(profile.context) as session:
            users = UserRepository(session)
            user = await users.get(profile.user_id)
            if user is None:
                raise NotFoundError("no such user")
            user.name = name.strip() or None
            await session.flush()
        return await self.describe(profile)

    async def change_password(self, profile: AccessProfile, current: str, new: str) -> None:
        """Let somebody change their own password, which until now nobody could.

        A password was generated at invitation, read out once, and could only ever be
        changed by an administrator with shell access running `reset-password`. For a
        product whose own documentation admits the invitation password travels through a
        chat message, being unable to replace it afterwards is the sharper end of that
        trade-off.

        The current password is required. Not theatre: an access token in someone else's
        hands is a session, and without this it would also be a permanent account
        takeover — change the password, and the real owner is locked out of their own
        tenant with no way back that does not involve an administrator.

        Bumping `token_version` signs the other sessions out, which is what a person
        changing a password is asking for even when they do not say so: the reason to
        change one is usually the suspicion that somebody else has it.

        **Not instantaneous, and the difference matters enough to state.** Access tokens
        are stateless by design (mvp.md 2.4) — verifying one touches no database, because
        a query in front of every request was judged too high a price for revocation
        latency the refresh path already bounds. So a live access token issued before the
        bump keeps working until it expires; what it cannot do is renew itself, because
        `refresh` compares the version and refuses. The window is therefore at most one
        access-token lifetime, and any interface offering this must say that rather than
        implying the sessions drop the moment the button is pressed.
        """
        async with tenant_session(profile.context) as session:
            users = UserRepository(session)
            user = await users.get(profile.user_id)
            if user is None:
                raise NotFoundError("no such user")
            if not verify_password(current, user.password_hash):
                raise AuthenticationError("that is not your current password")
            user.password_hash = hash_password(new)
            user.token_version += 1
            await session.flush()

    async def sign_out_everywhere(self, profile: AccessProfile) -> None:
        """End every session for this user, this one included.

        Same mechanism and the same caveat as `change_password`: raising `token_version`
        stops every existing token from being *renewed*, and a live access token survives
        until it expires. Revocation is bounded by the access-token lifetime rather than
        immediate — a deliberate trade in mvp.md 2.4, which chose not to put a database
        read in front of every request to shorten it.

        Worth knowing when reading this on a development machine: `backend/.env` sets
        `ZENITH_ACCESS_TOKEN_MINUTES=240` as a local convenience, so the window here is
        four hours rather than the fifteen minutes the code defaults to.
        """
        async with tenant_session(profile.context) as session:
            users = UserRepository(session)
            user = await users.get(profile.user_id)
            if user is None:
                raise NotFoundError("no such user")
            user.token_version += 1
            await session.flush()

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
