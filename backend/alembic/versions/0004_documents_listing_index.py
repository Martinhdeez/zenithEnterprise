"""index for the document listing order

Revision ID: 0004
Revises: 0003

`GET /documents` is keyset-paginated: newest first, resuming from a cursor on
`(created_at, id)`. Without an index in that exact order Postgres sorts the tenant's whole
visible corpus to return twenty rows, and the cost grows with the corpus rather than with
the page.

`id` is in the key because `created_at` is not unique — two documents inserted in one
transaction share a timestamp, and a cursor that cannot separate them either skips one or
returns it forever.

Descending on both columns to match the query. A btree can be read backwards, so an
ascending index would also work; stating the order the query uses keeps the two in step if
either changes later.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_documents_listing",
        "documents",
        ["tenant_id", "created_at", "id"],
        postgresql_ops={"created_at": "DESC", "id": "DESC"},
    )


def downgrade() -> None:
    op.drop_index("ix_documents_listing", table_name="documents")
