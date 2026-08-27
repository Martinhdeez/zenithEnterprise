"""searchable question history

Revision ID: 0014
Revises: 0013

`GET /query/history` gained a search box, and `ILIKE '%term%'` cannot use an ordinary
B-tree: a leading wildcard has no prefix to seek on, so every search is a sequential scan
over every question the tenant has ever asked. That is fine for a week and not for a year.

A trigram index is the one that answers this shape of query. `pg_trgm` is a standard
contrib extension and ParadeDB ships it; `IF NOT EXISTS` because an installation may
already have it for something else.

Deliberately not full-text search, though the machinery is right there — `chunks` carries a
tsvector and ParadeDB does BM25 over it. A history search is somebody looking for the
question they asked on Tuesday, and they will type a fragment of it: `severance` should find
"what is my severance?" and so should `sever`. Stemming and ranking answer a different
question well and this one badly, and a substring match is what the box in front of them
looks like it does.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # GIN rather than GiST: this index is read on every search and written once per question,
    # which is the trade GIN is built for. Concurrently is not used because Alembic runs
    # inside a transaction here, and a history table is small enough that the brief lock is
    # not what anybody notices about an upgrade.
    op.execute("CREATE INDEX ix_queries_question_trgm ON queries USING gin (question gin_trgm_ops)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_queries_question_trgm")
    # The extension stays. Something else may have come to depend on it, and dropping a
    # shared extension to undo one index is a bigger claim than this migration made.
