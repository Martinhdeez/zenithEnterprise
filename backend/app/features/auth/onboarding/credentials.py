"""Setting your own password, from a link.

Two problems, one mechanism.

**Onboarding.** `POST /users/invite` generated a password and showed it once. The
invitation handler already named the cost: *"a password passed through a chat message is a
password in a chat log"*. The credential never travelled less than fully — whatever channel
the administrator pasted it into now holds a working credential, permanently.

**Recovery.** A forgotten password meant `zenith reset-password` over SSH. That does not
survive a third customer, and it makes every forgotten password an escalation to whoever
holds the server key.

A link fixes both. It is single use, it expires, and the password it produces is chosen by
the person whose password it is and never leaves their browser. The same paste in the same
chat log is worthless a minute after it is used.

**Still no email.** The product ships into networks with no outbound SMTP — that is why
invitations never sent one — so the administrator copies a link exactly as they copied a
password before. What changed is what is in their clipboard, not their workflow. Wiring
SMTP later changes only who does the pasting.

**The token is never stored.** `token_hash` holds sha256 of it, and the row cannot produce
a working link. sha256 rather than bcrypt because this is 32 bytes from `secrets`, not a
human-chosen password: there is no dictionary for slow hashing to defend against, and the
lookup is on the interactive path.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text

from app.common.exceptions import NotFoundError
from app.core.database import tenant_session
from app.core.security import hash_password
from app.features.auth.service import AccessProfile
from app.features.auth.throttle import run_hash
from app.features.tenancy.context import TenantContext

#: 32 bytes of urlsafe base64. Long enough that guessing is not a threat model, short enough
#: to survive being pasted into a chat window without wrapping.
TOKEN_BYTES = 32

#: Long enough to cross a weekend, short enough that a link forgotten in a chat log stops
#: being a way in. An expiry measured in months would give back exactly what this replaced.
INVITATION_HOURS = 72

#: Shorter, because a reset is requested by somebody who is waiting for it right now, and a
#: recovery link outliving the afternoon it was needed is pure exposure.
RESET_HOURS = 4

INVITATION = "invitation"
RESET = "reset"


@dataclass(frozen=True, slots=True)
class IssuedLink:
    """What the administrator copies. Returned once and never recoverable."""

    token: str
    email: str
    purpose: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TokenSubject:
    """Who a link belongs to, for the page that redeems it."""

    email: str
    purpose: str


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue(
    context: TenantContext,
    user_id: UUID,
    purpose: str,
    issued_by: UUID | None = None,
) -> IssuedLink:
    """Mint a link for somebody in this tenant.

    Any outstanding link for the same person is retired first. Two live invitations mean two
    working ways in, and an administrator who reissued because the first went astray has
    every reason to believe the first one stopped working.
    """
    token = secrets.token_urlsafe(TOKEN_BYTES)
    hours = INVITATION_HOURS if purpose == INVITATION else RESET_HOURS
    expires = datetime.now(UTC) + timedelta(hours=hours)

    async with tenant_session(context) as session:
        email = await session.scalar(text("SELECT email FROM users WHERE id = :u"), {"u": user_id})
        if not email:
            raise NotFoundError("no such user in this organisation")

        await session.execute(
            text(
                "UPDATE credential_tokens SET used_at = now() "
                "WHERE user_id = :u AND used_at IS NULL"
            ),
            {"u": user_id},
        )
        await session.execute(
            text(
                "INSERT INTO credential_tokens "
                "(tenant_id, user_id, token_hash, purpose, expires_at, created_by) "
                "VALUES (:t, :u, :h, :p, :e, :b)"
            ),
            {
                "t": context.tenant_id,
                "u": user_id,
                "h": digest(token),
                "p": purpose,
                "e": expires,
                "b": issued_by,
            },
        )

    return IssuedLink(token=token, email=email, purpose=purpose, expires_at=expires)


async def issue_for(profile: AccessProfile, user_id: UUID, purpose: str) -> IssuedLink:
    return await issue(profile.context, user_id, purpose, issued_by=profile.user_id)


class CredentialTokens:
    """The unauthenticated half: reading and redeeming a link.

    Both calls go through SECURITY DEFINER functions from migration 0016. They have to: this
    request carries no token and therefore no tenant, so there is no context to bind a
    session to and RLS has nothing to filter on. The functions take a hash and return one
    row, which is the narrowest hole that does the job.
    """

    async def subject(self, token: str) -> TokenSubject:
        """Who this link is for, so the page can greet them.

        Expired, already used and never issued are one answer on purpose. Telling them apart
        would confirm to somebody guessing that a particular string was once real.
        """
        async with tenant_session(TenantContext(tenant_id=UUID(int=0))) as session:
            row = (
                await session.execute(
                    text("SELECT email, purpose FROM zenith_credential_token_lookup(:h)"),
                    {"h": digest(token)},
                )
            ).first()

        if row is None:
            raise NotFoundError("this link is no longer valid")
        return TokenSubject(email=row.email, purpose=row.purpose)

    async def redeem(self, token: str, password: str) -> None:
        """Spend the link and set the password, in one statement.

        Not two calls. Two means a window where the link is spent and no password was set,
        which locks somebody out while holding a link that no longer works.

        The function also bumps `token_version`, so every existing session for this account
        stops. On a reset that is the entire point: if the reason for resetting is that
        somebody else had the account, a new password that leaves their session alive has
        fixed nothing.
        """
        async with tenant_session(TenantContext(tenant_id=UUID(int=0))) as session:
            user_id = await session.scalar(
                text("SELECT zenith_credential_token_consume(:h, :p)"),
                {"h": digest(token), "p": await run_hash(lambda: hash_password(password))},
            )

        if user_id is None:
            raise NotFoundError("this link is no longer valid")
