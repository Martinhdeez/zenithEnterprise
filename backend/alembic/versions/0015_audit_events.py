"""an audit log that audits

Revision ID: 0015
Revises: 0014

The screen labelled "Audit Log" reads `queries`. It is the record of questions people
asked, which is worth having and is not an audit trail: nothing anywhere records who
granted a role, who moved somebody into a group, who raised a clearance level, who
suspended an organisation or who ordered a purge.

For a product whose differentiator is RBAC and ABAC with clearance levels, *"show me who
gave this person access to Legal, and when"* is the first question a compliance reviewer
asks, and today the honest answer is that the system does not know. That is the gap this
closes.

**Append-only, enforced by Postgres rather than by convention.** `zenith_app` gets INSERT
and SELECT and nothing else:

    REVOKE UPDATE, DELETE ON audit_events FROM zenith_app

An audit log the application can rewrite is not evidence of anything — a reviewer's next
question after "who granted this" is "could the record have been altered", and a policy
comment is not an answer to it. This is the same instrument 0010 used to keep
`users.is_system_admin` out of the application's reach, for the same reason: the guarantee
has to survive whatever bug ships next, so it lives in the grant table and not in a code
path somebody can forget.

Corrections are appended, never edited. That is what an append-only log means and it is
the property that makes it worth reading.

**`actor_email` is denormalised on purpose.** `actor_user_id` references `users` with
`ON DELETE SET NULL`, because the person who granted an access two years ago may since have
been deleted — and an audit row that loses the actor when the actor leaves records nothing
about the moment that matters most. The email is copied in at write time and stays.

**Tenant scope, with one deliberate hole.** Ordinary events carry `tenant_id` and are
readable under the usual policy. System-level events — provisioning, suspension, purging —
belong to no tenant and carry NULL; they are written and read through `platform_session`,
which already bypasses RLS and whose call sites are the complete audit surface of the
system panel. A purge writes its record *before* the rows are destroyed and the record
survives, since `audit_events.tenant_id` is `ON DELETE SET NULL`: the tombstone in
`tenants` says an organisation existed, and this says who ended it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "tenant_id = zenith_current_tenant()"
AUDIT_READ = "audit.read"


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Nullable: system-level events belong to no tenant. `SET NULL` rather than CASCADE
        # so purging an organisation does not destroy the record of it being purged.
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Copied at write time. Survives the actor's deletion, which is exactly when an
        # audit trail earns its keep.
        sa.Column("actor_email", sa.String(), nullable=False),
        # A stable token, `subject.verb`: `role.created`, `group.members_set`,
        # `tenant.purged`. Read by machines as well as people, so it never carries a
        # rendered sentence — the UI writes the prose from this and `details`.
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("target_type", sa.String(), nullable=True),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        # What the target was called at the time. A role renamed or deleted afterwards would
        # otherwise leave a row naming a uuid nobody can resolve.
        sa.Column("target_name", sa.String(), nullable=True),
        # What changed, shaped per action. JSONB rather than columns because the actions do
        # not share a shape: a permission change carries two lists, a clearance change two
        # integers, a purge a document count.
        sa.Column(
            "details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # The read pattern is one tenant's log, newest first, in keyset pages — the same shape
    # `queries` is paginated by, and for the same reason: `OFFSET n` re-evaluates the policy
    # on every discarded row.
    op.create_index(
        "ix_audit_events_tenant_created",
        "audit_events",
        ["tenant_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )

    op.execute("ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_events FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY audit_events_isolation ON audit_events "
        f"USING ({POLICY}) WITH CHECK ({POLICY})"
    )

    # The narrowing that makes this a record rather than a table. 0001 granted the
    # application UPDATE and DELETE on everything; both come off here, and only here.
    op.execute("REVOKE UPDATE, DELETE ON audit_events FROM zenith_app")
    op.execute("GRANT SELECT, INSERT ON audit_events TO zenith_app")

    # `zenith_platform` needs the same, and needs it stated here. 0010 granted that role
    # every table *that existed then*; a table created afterwards is invisible to it, and
    # the symptom is not a startup failure but a system-level event silently failing to be
    # written — the purge going unrecorded, which is the one event that matters most.
    #
    # Append-only for the platform role too. Bypassing RLS is a different power from being
    # allowed to edit history, and the system panel needs the first, not the second.
    op.execute("REVOKE UPDATE, DELETE ON audit_events FROM zenith_platform")
    op.execute("GRANT SELECT, INSERT ON audit_events TO zenith_platform")

    # The catalogue lives in two places on purpose — `permissions.py` is the readable copy,
    # this table is the authoritative one — and `test_catalogue_matches_the_database` fails
    # the moment they drift. Adding the code to one without the other breaks that test,
    # which is the guard working.
    op.get_bind().execute(
        sa.text("INSERT INTO permissions (code, description) VALUES (:code, :description)"),
        {"code": AUDIT_READ, "description": "Read the record of who changed access to what"},
    )

    # Existing tenants have an `admin` role that was seeded with every permission that
    # existed at the time. Without this, the people who most need the new screen are the
    # only ones who cannot open it, and the failure looks like a bug in the panel rather
    # than a permission that was never granted.
    op.execute(
        f"""
        INSERT INTO role_permissions (role_id, permission_code)
        SELECT r.id, '{AUDIT_READ}' FROM roles r
        WHERE r.name = 'admin' AND r.is_system
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM role_permissions WHERE permission_code = '{AUDIT_READ}'")
    op.execute(f"DELETE FROM permissions WHERE code = '{AUDIT_READ}'")
    op.execute("DROP POLICY IF EXISTS audit_events_isolation ON audit_events")
    op.drop_index("ix_audit_events_tenant_created", table_name="audit_events")
    op.drop_table("audit_events")
