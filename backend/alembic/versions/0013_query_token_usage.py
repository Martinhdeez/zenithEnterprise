"""token counts on the query log

Revision ID: 0013
Revises: 0012

`queries` has recorded the question, the answer, the model and both latencies since 0001.
What it could not answer is what any of it cost, which is the first thing asked of an
installation paying a per-token bill.

**Nullable, and staying that way.** `GenerationResponse` explains the reasoning it inherits:
a local llama.cpp binding reports no usage at all, and a gateway is free to strip it. NULL
means "the provider did not say", which is not zero — and a dashboard that renders one as
the other puts a confident, wrong number in front of somebody who will budget against it.

No backfill. Every row written before this migration was answered by a provider that was
never asked for its usage, and inventing a number for them from character counts would make
the old rows indistinguishable from the new ones while being wrong about all of them.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("queries", sa.Column("prompt_tokens", sa.Integer(), nullable=True))
    op.add_column("queries", sa.Column("completion_tokens", sa.Integer(), nullable=True))

    # The analytics dashboard's every query is "this tenant, this window, ordered by time".
    # Without this it is a sequential scan over every question the tenant has ever asked,
    # on a screen an administrator refreshes.
    op.create_index(
        "ix_queries_tenant_created",
        "queries",
        ["tenant_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_queries_tenant_created", table_name="queries")
    op.drop_column("queries", "completion_tokens")
    op.drop_column("queries", "prompt_tokens")
