"""every tenant has a default label again

Revision ID: 0012
Revises: 0011

A tenant is provisioned with one and there is no supported way to remove it. A tenant was
found without one anyway — deleted through the label management screen, which happily
removes any label including that one.

The symptom is total and the message is useless. `DocumentService` refuses every upload that
names no label, because storing a document with an empty `label_ids` publishes it to the
whole tenant by omission rather than by decision. So the product's most common action stops
working, and it fails with "this tenant has no default access label", a sentence about a
concept nobody outside this codebase has heard of.

This backfills the missing ones. `app/features/labels/provisioning.ensure_default_label`
repairs the same thing at runtime, so an installation cannot be locked out again between
upgrades — the two paths create the identical thing: a label named `General`, marked
default, granted to the tenant's system roles and to nothing else.

**It grants nothing wider than the tenant already had.** The label reaches exactly the roles
provisioning would have given it, so no document becomes visible to anybody who could not
have seen an unclassified upload the day the tenant was created. Written as raw SQL rather
than by importing the model, which is the rule for migrations here: a migration that imports
application code breaks the day that code is refactored, and it has to keep running against
the schema as it was.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Matches `labels.provisioning.DEFAULT_LABEL`. Duplicated deliberately — see the module
#: docstring on why a migration does not import application code.
DEFAULT_LABEL = "General"


def upgrade() -> None:
    bind = op.get_bind()

    # A purged tenant is skipped: its rows are gone on purpose and giving it a label would
    # put one row back into an organisation somebody deliberately emptied.
    orphans = list(
        bind.execute(
            sa.text(
                "SELECT t.id FROM tenants t "
                "WHERE t.status <> 'purged' "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM access_labels l "
                "    WHERE l.tenant_id = t.id AND l.is_default"
                "  )"
            )
        )
    )

    for row in orphans:
        label_id = bind.execute(
            sa.text(
                "INSERT INTO access_labels (tenant_id, name, is_default) "
                "VALUES (:t, :name, true) "
                # A tenant may already have a label called `General` that simply is not
                # marked default — the screen can clear the flag as well as delete the row.
                # Promoting it is better than failing on the unique name.
                "ON CONFLICT (tenant_id, name) DO UPDATE SET is_default = true "
                "RETURNING id"
            ),
            {"t": row.id, "name": DEFAULT_LABEL},
        ).scalar_one()

        bind.execute(
            sa.text(
                "INSERT INTO role_labels (role_id, label_id) "
                "SELECT r.id, :l FROM roles r WHERE r.tenant_id = :t AND r.is_system "
                "ON CONFLICT DO NOTHING"
            ),
            {"l": label_id, "t": row.id},
        )


def downgrade() -> None:
    """Deliberately empty.

    Removing the labels this created would put the installation back into the state that
    made the migration necessary — uploads refused, for a reason nobody can act on. A
    downgrade is for undoing a schema change; this one changed no schema.
    """
