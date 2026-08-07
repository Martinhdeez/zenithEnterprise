"""optional description on documents

Revision ID: 0006
Revises: 0005

The upload screen only ever asked for a file and labels. `filename` already carries a
name — the file's own — but nothing let a user override it, and there was nowhere to add
context a filename can't carry ("Q3 draft, superseded by the signed version"). Both are
UI-facing conveniences, not something ingestion or search reads, so a nullable column is
enough: no backfill, no default, existing rows are simply undescribed.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "description")
