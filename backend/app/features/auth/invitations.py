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

## What the response deliberately does not do

It does not say whether the address already existed anywhere else. Within a tenant a
duplicate is a plain conflict, because the administrator is entitled to know about their own
users. Across tenants it must not be one: `users` is unique per `(tenant_id, email)`
precisely so that two customers may employ the same person, and an error that distinguished
"taken here" from "taken elsewhere" would leak one customer's staff list to another.
"""

import secrets
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.common.exceptions import ConflictError, NotFoundError
from app.core.database import tenant_session
from app.features.auth.provisioning import create_user
from app.features.auth.service import AccessProfile

INVITE = "users.invite"

#: Long enough that it need never be memorised, and generated rather than chosen. An
#: administrator inventing passwords for colleagues produces the same one twice.
PASSWORD_BYTES = 18


@dataclass(frozen=True, slots=True)
class Invitation:
    user_id: UUID
    email: str
    #: Returned exactly once, never stored in the clear and never logged. If it is lost the
    #: remedy is to invite again, which is cheap; recovering it is impossible by design.
    password: str
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

            try:
                user = await create_user(session, self.context.tenant_id, email, password, role_ids)
            except IntegrityError as exc:
                # Within the tenant this is an ordinary conflict and the administrator is
                # entitled to it — they can see their own users. The uniqueness constraint
                # is per `(tenant_id, email)`, so this can never fire for an address that
                # only exists in another customer's tenant.
                raise ConflictError(f"{email} is already a user here") from exc

        return Invitation(
            user_id=user.id,
            email=user.email,
            password=password,
            role_ids=list(role_ids),
        )
