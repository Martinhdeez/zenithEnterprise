"""one address, one account, installation-wide

Revision ID: 0011
Revises: 0010

`users` was unique per `(tenant_id, email)`, which let the same address exist in two
organisations. That was a deliberate choice — two customers may employ the same person —
and it had a consequence nobody had traced: **neither account could log in.**

`AuthService.authenticate` looks a user up by address and treats anything other than exactly
one match as a failed login. Two matches is not one, so both accounts answer 401 forever.
The only evidence is a `login_ambiguous_email` line in the server log. Reproduced before
writing this: the same address in two organisations, both passwords correct, both refused.

So the address becomes unique installation-wide, and the ambiguity that broke login cannot
be created.

**This is a trade, not a free win, and the losing side is worth naming.** Refusing to create
an address that exists elsewhere tells the administrator making the request that it exists
elsewhere — by the refusal itself, whatever the message says. On a hosted install, one
customer's administrator can now test whether a person has an account with another customer.
That is a real disclosure, and it is the reason the old design allowed duplicates. It is
accepted here because the alternative on the table is worse: an account that silently cannot
be used, with no error anybody but an operator reading logs can see.

The constraint is in the database rather than in a check before each insert. Three code
paths create users — the invitation endpoint, the system panel's provisioning, and the CLI —
and a rule enforced in three places is a rule enforced in two places as soon as somebody
adds a fourth.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Asked before the index is built, so an installation that already has duplicates gets a
    # sentence naming them rather than Postgres's "could not create unique index" and a
    # constraint name. Whoever runs this upgrade is the person who has to go and resolve
    # them, and they need to know which.
    duplicates = list(
        op.get_bind().execute(
            sa.text(
                "SELECT email, count(*) AS accounts FROM users "
                "GROUP BY email HAVING count(*) > 1 ORDER BY email"
            )
        )
    )
    if duplicates:
        listed = ", ".join(f"{row.email} ({row.accounts} accounts)" for row in duplicates)
        raise RuntimeError(
            "These addresses exist in more than one organisation, and none of them can log "
            f"in today: {listed}. Note that this migration is not what broke them — "
            "`AuthService.authenticate` refuses an ambiguous address and always has. "
            "Rename or remove the duplicates, then run this upgrade again."
        )

    # The per-tenant constraint from 0001 stays. It is now implied by this one, and it is
    # also the index that serves `WHERE tenant_id = ... AND email = ...`, so dropping it
    # would trade a redundancy for a sequential scan.
    op.create_index("uq_users_email_global", "users", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_users_email_global", table_name="users")
