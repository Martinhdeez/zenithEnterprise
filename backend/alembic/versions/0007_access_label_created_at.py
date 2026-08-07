"""created_at on access_labels

Revision ID: 0007
Revises: 0006

`GET /labels/search` sorts by name, usage or recency. The first two are derivable from
what already exists — `name` is a column, usage is a count over `document_labels` — but
recency had nothing to sort on: `access_labels` has carried only `id`, `tenant_id`, `name`
and `is_default` since 0001.

Two honest caveats about the backfill:

Existing rows all collapse to the migration's own timestamp, because that is the only
value available — nothing recorded when a label was created, so there is nothing to
recover. Sorting an already-populated tenant by `created_at` therefore orders the
pre-migration labels arbitrarily among themselves (the `id` tiebreak in the keyset cursor
makes that ordering *stable*, just not *meaningful*). Labels created after this migration
sort correctly.

`server_default` rather than a Python-side default so a row inserted by a migration, a
repair script or psql gets a timestamp too. A default that only exists in the ORM is a
NOT NULL violation waiting for the first caller that does not go through it.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "access_labels",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # The sort order `GET /labels/search` actually issues, `id` included because
    # `created_at` is not unique — this migration alone gives every pre-existing row the
    # same value, so a cursor that could not separate them would loop or skip.
    op.create_index(
        "ix_access_labels_recency",
        "access_labels",
        ["tenant_id", "created_at", "id"],
        postgresql_ops={"created_at": "DESC", "id": "DESC"},
    )


def downgrade() -> None:
    op.drop_index("ix_access_labels_recency", table_name="access_labels")
    op.drop_column("access_labels", "created_at")
