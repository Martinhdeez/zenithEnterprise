"""tenant default label and the label denormalisation triggers

Revision ID: 0003
Revises: 0002

`documents.label_ids` and `chunks.label_ids` are copies of `document_labels`. They exist
because a policy on `documents` that read `document_labels`, whose own policy reads
`documents`, makes Postgres abort on mutual recursion — the copy is not an optimisation,
the direct version does not run.

That makes the copy load-bearing for security, and a copy that drifts from its source is a
leak or an outage depending on which direction it drifts. So it is maintained here, by the
database, rather than by application code that every future path has to remember to call.
Same argument that chose RLS over `WHERE` clauses.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Both functions are SECURITY DEFINER, deliberately.
#
# The policy on `documents` carries WITH CHECK (label_ids = '{}' OR label_ids &&
# zenith_current_labels()). Without the bypass, an administrator holding `labels.manage`
# who does not personally reach "Finance" would be blocked from removing the last
# non-Finance label from a document, because the resulting row is one they can no longer
# see — an error about a row they never mentioned.
#
# The trigger is not an access decision. It maintains a derived value. The access decision
# belongs where the user acts: RLS on `document_labels` governs whether they may change
# the mapping at all, and `requires("labels.manage")` governs whether they reach the
# endpoint. Deciding it again here means deciding it twice, in the place with the least
# information.
#
# Recorded in technical-decisions.md §5.1, which is the complete list of the routes around
# RLS. A list that is quietly incomplete is worse than no list at all.
_SYNC_DOCUMENT_LABELS = """
CREATE FUNCTION zenith_sync_document_labels()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    affected uuid := COALESCE(NEW.document_id, OLD.document_id);
BEGIN
    UPDATE documents d
    SET label_ids = COALESCE(
        (SELECT array_agg(dl.label_id ORDER BY dl.label_id)
         FROM document_labels dl
         WHERE dl.document_id = affected),
        '{}'::uuid[]
    )
    WHERE d.id = affected;
    RETURN NULL;
END;
$$
"""

# Chunks carry their own copy so the tenant and label filters apply inside the vector
# query: a join there penalises the HNSW index.
#
# `IS DISTINCT FROM` is not a micro-optimisation. Without it, every status transition
# during ingestion — pending, parsing, chunking, embedding, ready — rewrites every chunk
# row of the document, five times per upload, for a value that did not change.
_SYNC_CHUNK_LABELS = """
CREATE FUNCTION zenith_sync_chunk_labels()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    IF NEW.label_ids IS DISTINCT FROM OLD.label_ids THEN
        UPDATE chunks SET label_ids = NEW.label_ids WHERE document_id = NEW.id;
    END IF;
    RETURN NULL;
END;
$$
"""


# A chunk is created long after its document was labelled: ingestion parses, chunks and
# embeds minutes later. Without this, every chunk is born with an empty label array — and
# an empty array means visible to the whole tenant, so a passage from a Finance document
# would be retrievable by everyone while the document itself stayed hidden.
#
# The alternative is for the ingestion worker to remember to copy the labels. That is the
# discipline this whole design exists to avoid depending on, and the worker does not exist
# yet to be reviewed.
_FILL_CHUNK_LABELS = """
CREATE FUNCTION zenith_fill_chunk_labels()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    IF NEW.label_ids IS NULL OR NEW.label_ids = '{}'::uuid[] THEN
        SELECT d.label_ids INTO NEW.label_ids FROM documents d WHERE d.id = NEW.document_id;
    END IF;
    RETURN NEW;
END;
$$
"""


def upgrade() -> None:
    # The uploader must choose a label at upload time (mvp.md §2.2), with a default the
    # tenant administrator configures. Without it, F4 has to invent one or leave documents
    # unlabelled — and unlabelled means visible to the whole tenant.
    #
    # The flag lives on the label rather than as `tenants.default_label_id`, which would
    # close a foreign-key cycle with `access_labels.tenant_id`. A cycle is not merely
    # untidy: SQLAlchemy cannot order the two tables, and it warns that it will stop
    # tolerating the situation.
    op.add_column(
        "access_labels",
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="false"),
    )
    # One default per tenant, enforced here rather than by application code that every
    # future path has to remember.
    op.create_index(
        "uq_access_labels_one_default_per_tenant",
        "access_labels",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    op.execute(_SYNC_DOCUMENT_LABELS)
    op.execute(_SYNC_CHUNK_LABELS)
    op.execute(_FILL_CHUNK_LABELS)

    # FOR EACH ROW on both INSERT and DELETE, so a label removed by the cascade from
    # `access_labels` updates the array too. A statement-level trigger would not see which
    # documents the cascade touched.
    op.execute(
        """
        CREATE TRIGGER document_labels_sync
        AFTER INSERT OR UPDATE OR DELETE ON document_labels
        FOR EACH ROW EXECUTE FUNCTION zenith_sync_document_labels()
        """
    )
    op.execute(
        """
        CREATE TRIGGER documents_label_propagation
        AFTER UPDATE OF label_ids ON documents
        FOR EACH ROW EXECUTE FUNCTION zenith_sync_chunk_labels()
        """
    )
    op.execute(
        """
        CREATE TRIGGER chunks_inherit_labels
        BEFORE INSERT ON chunks
        FOR EACH ROW EXECUTE FUNCTION zenith_fill_chunk_labels()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER chunks_inherit_labels ON chunks")
    op.execute("DROP TRIGGER documents_label_propagation ON documents")
    op.execute("DROP TRIGGER document_labels_sync ON document_labels")
    op.execute("DROP FUNCTION zenith_fill_chunk_labels()")
    op.execute("DROP FUNCTION zenith_sync_chunk_labels()")
    op.execute("DROP FUNCTION zenith_sync_document_labels()")
    op.drop_index("uq_access_labels_one_default_per_tenant", table_name="access_labels")
    op.drop_column("access_labels", "is_default")
