"""The quarantine label kept the grants it already had.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-25

Migration 0017 seeds `Unclassified` with `ON CONFLICT (tenant_id, name) DO UPDATE SET
is_quarantine = true`, so a tenant that already had a label of that name has it *adopted*
rather than replaced. Its comment claimed this was not a widening "because the grant below is
admin-only either way".

That is wrong, and it is wrong in the direction that matters. The `INSERT ... ON CONFLICT DO
NOTHING` beneath it *adds* the admin grant; it removes nothing. An adopted label that had been
granted to `member` — which is exactly what somebody would do with a label called
`Unclassified` — keeps that grant and becomes the place every unfiled upload lands. The leak
0017 was written to close, reopened by the migration closing it.

## What this does

Revokes every role grant on the quarantine label except `admin`'s, in every tenant. Nothing
else: the label keeps its name, its documents and its identity.

**Narrowing, and worth being explicit that it can hide something.** If an adopted label
carries documents, the roles losing the grant lose sight of those documents. That is the same
direction 0017 chose deliberately and the same one every policy in this schema fails in — and
the repair is visible: an administrator still reaches the label, sees what is in it, and can
move those documents to a compartment of their own. The alternative is a reserved label that
half the tenant can read, which is not a reserved label.

The application refuses these grants from now on — `LabelService._refuse_if_reserved` — so
this is a one-time repair of what the schema allowed before the rule existed.
"""

import sqlalchemy as sa

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    revoked = bind.execute(
        sa.text(
            "DELETE FROM role_labels rl "
            "USING access_labels l, roles r "
            "WHERE rl.label_id = l.id AND rl.role_id = r.id "
            "  AND l.is_quarantine "
            "  AND NOT (r.is_system AND r.name = 'admin') "
            "RETURNING rl.role_id"
        )
    )
    count = len(list(revoked))
    if count:
        # Printed rather than silent. A migration that quietly removes access is worse than
        # one that says how much, especially when the answer is normally zero.
        print(f"0020: revoked {count} non-admin grant(s) on the quarantine label")


def downgrade() -> None:
    """Deliberately empty.

    The grants this removed were a defect, and there is no record of which they were. Putting
    an approximation of them back would be inventing access, which is the one thing a
    downgrade of an access migration must not do.
    """
