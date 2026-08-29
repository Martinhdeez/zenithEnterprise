"""Onboarding a colleague without a shell on the customer's server.

Until now the only way to create a user was `zenith create-user` on the box. That is fine
for the first administrator — somebody has to bootstrap — and absurd for the fourth
employee, which is what `users.invite` has always been in the catalogue for.

## No email, and therefore a password shown once

This product ships on-premise into networks that frequently have no outbound SMTP, and
requiring a mail server to add a colleague would make the feature undeployable exactly where
the product is sold. So there is no invitation link and no token to expire.

Instead the API **generates a password, returns it exactly once**, and never stores or logs
it in the clear. The administrator passes it on however they already pass on credentials.
This is the same decision the CLI made — it prints the password rather than accepting one,
because an argument ends up in shell history and the process table.

The cost is honest and worth writing down: a password that travels through a chat message
is a password in a chat log. The eventual answer is an invitation token and a set-password
page, which needs a public unauthenticated route and a token table — a milestone, not a
line. Recorded rather than pretended away.

## One address, one account — and what that costs

This module used to argue the opposite, and the argument was good: `users` was unique per
`(tenant_id, email)` so two customers could employ the same person, and an error telling one
administrator that an address was "taken elsewhere" would leak the other customer's staff.

It was wrong about the premise. A duplicate address did not produce two working accounts, it
produced two broken ones: `AuthService.authenticate` treats anything other than exactly one
match as a failed login, so both sides answered 401 forever and the only trace was a log
line. Migration 0011 makes the address unique installation-wide.

The leak the old design avoided is now real and is accepted deliberately. Refusing to create
an address that exists elsewhere reveals that it exists elsewhere — by the refusal itself,
whatever wording the message uses; a vaguer message would hide the reason from the
administrator without hiding the signal from anybody probing for it. The trade is a
disclosure an administrator can probe for, against an account that silently cannot be used.
"""

import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.common.exceptions import ConflictError, NotFoundError
from app.core.database import tenant_session
from app.features.auth.onboarding.credentials import INVITATION, issue
from app.features.auth.onboarding.provisioning import create_user
from app.features.auth.service import AccessProfile, normalise_email

INVITE = "users.invite"

#: Long enough that it need never be memorised, and generated rather than chosen. An
#: administrator inventing passwords for colleagues produces the same one twice.
PASSWORD_BYTES = 18


@dataclass(frozen=True, slots=True)
class Invitation:
    user_id: UUID
    email: str
    #: The single-use link the administrator hands over, returned exactly once. This used to
    #: be a generated password, and the difference is what ends up in a chat log: a password
    #: works forever and is the user's real credential, while this is spent on first use and
    #: expires on its own. The password it produces is chosen by the person whose password
    #: it is and never travels.
    token: str
    expires_at: datetime
    role_ids: list[UUID]


class InvitationService:
    def __init__(self, profile: AccessProfile) -> None:
        self.profile = profile
        self.context = profile.context

    async def invite(self, email: str, role_ids: list[UUID]) -> Invitation:
        password = secrets.token_urlsafe(PASSWORD_BYTES)

        async with tenant_session(self.context) as session:
            # Roles are validated first so a bad request cannot leave a user with no roles
            # and an administrator wondering whether the invitation half-worked.
            if role_ids:
                from sqlalchemy import text

                found = await session.scalars(
                    text("SELECT id FROM roles WHERE id = ANY(:ids)"), {"ids": role_ids}
                )
                known = set(found)
                missing = [str(role_id) for role_id in role_ids if role_id not in known]
                if missing:
                    # 404 rather than 403: a role in another tenant does not exist from
                    # here, and the difference between the two answers would confirm it.
                    raise NotFoundError(f"no role(s): {', '.join(missing)}")

            # Asked before the insert so the two conflicts can be told apart in the
            # message. Under RLS this sees only this tenant's users, so a match here is
            # unambiguously one of the administrator's own — and a miss followed by a
            # constraint violation is unambiguously somebody else's.
            from sqlalchemy import text

            here = await session.scalar(
                text("SELECT 1 FROM users WHERE email = :e"), {"e": normalise_email(email)}
            )
            if here:
                raise ConflictError(f"{email} is already a user here")

            try:
                user = await create_user(session, self.context.tenant_id, email, password, role_ids)
            except IntegrityError as exc:
                # The global index from 0011. The address exists in another organisation,
                # and one address is one account across the installation — see this
                # module's docstring for why, and for what it costs.
                raise ConflictError(
                    f"{email} is already registered in another organisation"
                ) from exc

        # The account exists with a password nobody knows — not even the administrator who
        # created it. `create_user` requires one, so it gets 32 bytes of noise that is
        # discarded here and can never be recovered. The only way in is the link.
        link = await issue(self.context, user.id, INVITATION, issued_by=self.profile.user_id)

        return Invitation(
            user_id=user.id,
            email=user.email,
            token=link.token,
            expires_at=link.expires_at,
            role_ids=list(role_ids),
        )
