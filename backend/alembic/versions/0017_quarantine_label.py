"""Close the window where an unfiled document is visible to the whole tenant.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-25

An upload that names no compartment is stored under the tenant's default label, because the
alternative — an empty `label_ids` — satisfies `label_ids = '{}'` and publishes the document
to everybody. The default is granted to both system roles, `admin` and `member`, so in
practice "unfiled" has meant "tenant-wide" from the moment `POST /documents` answered.

The classifier is what eventually narrows it, and it runs at the *end* of ingestion, after
the chunks are committed. So the window is not the length of one model call: it is the whole
ingestion, minutes per document on `low-spec`, and it covers listing the row and downloading
the PDF, not merely searching it. Worse, when the classifier chose nothing the document kept
the default label permanently, silently.

## The design

A second reserved label per tenant, `Unclassified`, granted to `admin` and to nobody else.
An upload that names no compartment lands there instead of in the default, and the classifier
moves it out.

`is_quarantine` mirrors `is_default`: a flagged column with a partial unique index, rather
than matching on the name. The name is a display string an administrator may rename, and an
access decision that depends on a string somebody can edit is not an access decision.

**The set of documents that get quarantined is exactly the set the classifier is allowed to
touch.** `IngestionPipeline._file` already computes it — a document whose labels are exactly
the tenant default, which is what "nobody chose" looks like. Reusing that condition rather
than inventing a second one is the point: the two ends cannot drift apart into a document
that is quarantined and never reclassified, or reclassified without ever being protected.

## The uploader exception

Quarantine alone would mean a `member` cannot see their own upload for the length of its
ingestion — the row vanishes from Documents and the upload screen's status poll starts
returning "not found". So the `documents` policy gains a third disjunct:

    uploaded_by = zenith_current_user_id() AND status <> 'ready'

Three things about it are deliberate.

**It is a read exception, not a write one.** This is the first time a document is reachable
by anything other than a label, so the policy stops using one condition for both directions:
`USING` gets the exception, `WITH CHECK` keeps the original label rule. Nobody gains the
ability to *create* a document under a compartment they do not hold — `DocumentService`
refuses that too, but the database must not be the layer that relies on it.

**It expires.** `status <> 'ready'` bounds it to the ingestion window instead of granting a
permanent second route to a document. Once the document is filed, only labels answer — so an
uploader who is later removed from a compartment loses the document they put there, which is
what a compartment means.

**It does not extend to chunks.** `chunks` and `chunk_embeddings` keep their label-only
policies, so retrieval is reachable by label and nothing else. The exception exists to let
somebody track a file they are uploading; it is not a way into the search index.

If the classifier fails, the document stays quarantined and `status_detail` says so. An
administrator files it by hand. That is a document nobody can read rather than a document
everybody can, which is the direction every other policy here fails in.
"""

import sqlalchemy as sa

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

#: Matches `labels.provisioning.QUARANTINE_LABEL`. Duplicated deliberately: a migration
#: describes the schema at one moment in history and must not change when the application's
#: constant does.
QUARANTINE_LABEL = "Unclassified"

#: What `documents` rows a session may read. The first two disjuncts are 0001 unchanged.
DOCUMENTS_READ = (
    "tenant_id = zenith_current_tenant() AND ("
    "  label_ids = '{}'::uuid[]"
    "  OR label_ids && zenith_current_labels()"
    "  OR (uploaded_by = zenith_current_user_id() AND status <> 'ready')"
    ")"
)

#: What it may write. 0001's condition, untouched — see the module docstring.
DOCUMENTS_WRITE = (
    "tenant_id = zenith_current_tenant() AND "
    "(label_ids = '{}'::uuid[] OR label_ids && zenith_current_labels())"
)


def upgrade() -> None:
    op.add_column(
        "access_labels",
        sa.Column("is_quarantine", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_index(
        "uq_access_labels_one_quarantine_per_tenant",
        "access_labels",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("is_quarantine"),
    )

    op.execute("DROP POLICY documents_isolation ON documents")
    op.execute(
        f"CREATE POLICY documents_isolation ON documents "
        f"USING ({DOCUMENTS_READ}) WITH CHECK ({DOCUMENTS_WRITE})"
    )

    bind = op.get_bind()

    # Every tenant that does not have one yet. A purged tenant is skipped for the reason 0012
    # gives: its rows are gone on purpose, and seeding one would put a row back into an
    # organisation somebody deliberately emptied.
    tenants = list(
        bind.execute(
            sa.text(
                "SELECT t.id FROM tenants t "
                "WHERE t.status <> 'purged' "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM access_labels l "
                "    WHERE l.tenant_id = t.id AND l.is_quarantine"
                "  )"
            )
        )
    )

    for row in tenants:
        label_id = bind.execute(
            sa.text(
                "INSERT INTO access_labels (tenant_id, name, is_quarantine) "
                "VALUES (:t, :name, true) "
                # A tenant may already have a label of this name that an administrator
                # created by hand. Flagging it is better than failing on the unique name.
                #
                # This comment used to add "and it is not a widening, because the grant below
                # is admin-only either way", which was wrong: the grant below *adds* admin and
                # removes nothing, so an adopted label keeps whatever it was already granted
                # to. Migration 0020 repairs that, and `LabelService` refuses such a grant
                # from now on.
                "ON CONFLICT (tenant_id, name) DO UPDATE SET is_quarantine = true "
                "RETURNING id"
            ),
            {"t": row.id, "name": QUARANTINE_LABEL},
        ).scalar_one()

        # `admin` alone. Granting it to every system role would make it reach exactly as far
        # as the default label it replaces, which would be an elaborate way of changing
        # nothing.
        bind.execute(
            sa.text(
                "INSERT INTO role_labels (role_id, label_id) "
                "SELECT r.id, :l FROM roles r "
                "WHERE r.tenant_id = :t AND r.is_system AND r.name = 'admin' "
                "ON CONFLICT DO NOTHING"
            ),
            {"l": label_id, "t": row.id},
        )


def downgrade() -> None:
    op.execute("DROP POLICY documents_isolation ON documents")
    op.execute(
        f"CREATE POLICY documents_isolation ON documents "
        f"USING ({DOCUMENTS_WRITE}) WITH CHECK ({DOCUMENTS_WRITE})"
    )
    op.drop_index("uq_access_labels_one_quarantine_per_tenant", table_name="access_labels")
    op.drop_column("access_labels", "is_quarantine")
