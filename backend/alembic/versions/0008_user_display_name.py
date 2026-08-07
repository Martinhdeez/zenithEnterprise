"""display name on users

Revision ID: 0008
Revises: 0007

A user has been an email and a password hash since 0001, which is enough to authenticate
somebody and not enough to greet them. The profile screen shows who you are signed in as,
the avatar shows an initial, and a shared query history says whose question was whose —
all three were reduced to reading an email address, or in the avatar's case to a hardcoded
letter that looked like a person's initial and was the product's own.

Nullable, and staying that way. Existing users have no name to backfill, and an invitation
that demanded one would put a required field in front of an administrator who may only
know the address they were told to add. Everything that renders it falls back to the email,
which is the value that is always there.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("name", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "name")
