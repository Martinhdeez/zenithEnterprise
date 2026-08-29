"""A document is not `ready` until its labels are decided.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-25

Migration 0017 gave the `documents` policy a clause letting an uploader read their own
document while it is still ingesting:

    uploaded_by = zenith_current_user_id() AND status <> 'ready'

and `_persist` wrote `status = 'ready'` in the same transaction as the chunks — *before*
`_file` asks a model where the document belongs. So during the one step the exception was
written for, it was already switched off. A member who uploaded a file lost it from Documents
while the classifier ran, and the upload screen's poller started reporting "not found". If the
filing then failed, the document stayed quarantined and they never got it back.

`classifying` closes that. It sits between `embedding` and `ready`, so `status <> 'ready'`
covers the model call, and the invariant the pipeline is built on is untouched: `ready` still
means the chunks are committed, because it is now written strictly later than they are rather
than merely with them.

It is a fifth in-flight status rather than leaving the document at `embedding` through the
call. Both close the hole; only one of them is true. `IN_FLIGHT` is derived from this tuple
rather than listed separately — F16 shipped a folder count filtering on a status named
`processing` that had never existed and matched nothing, silently — so adding it here is
enough for every count and filter that asks "still being worked on".
"""

import sqlalchemy as sa

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

BEFORE = ("pending", "parsing", "chunking", "embedding", "ready", "failed")
AFTER = ("pending", "parsing", "chunking", "embedding", "classifying", "ready", "failed")


#: Wrapped in `op.f` at every use. Alembic applies the metadata naming convention to a bare
#: string, so `"ck_documents_status_valido"` becomes `ck_documents_ck_documents_status_valido`
#: — a constraint that does not exist, and a migration that fails on a fresh database while
#: passing every type check.
NAME = "ck_documents_status_valido"


def upgrade() -> None:
    op.drop_constraint(op.f(NAME), "documents", type_="check")
    op.create_check_constraint(op.f(NAME), "documents", f"status IN {AFTER}")


def downgrade() -> None:
    # Any document caught mid-classification would violate the older constraint. Settling it
    # to `ready` is correct rather than convenient: its chunks are committed — that is what
    # reaching this status means — so it is searchable, and the only thing left undecided is
    # which labels a model would have chosen.
    op.execute(sa.text("UPDATE documents SET status = 'ready' WHERE status = 'classifying'"))
    op.drop_constraint(op.f(NAME), "documents", type_="check")
    op.create_check_constraint(op.f(NAME), "documents", f"status IN {BEFORE}")
