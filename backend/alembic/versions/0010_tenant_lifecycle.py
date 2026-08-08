"""tenant lifecycle and the system administrator

Revision ID: 0010
Revises: 0009

An organisation could be created from the CLI and could never be taken down: no status
column, no delete, nothing anywhere. A customer who left kept their documents, embeddings
and query history indefinitely, and a formal erasure request was answered by hand in psql —
which does not touch the files on disk either.

Three things land here, and each one exists to make a different mistake impossible.

**`users.is_system_admin`, and the grant that protects it.** The flag cannot be a permission
in `CATALOGUE`, because a tenant's administrator edits `role_permissions` freely from the
roles screen and would be able to promote themselves out of their own tenant. Putting it on
a column is not enough on its own either — so the blanket `GRANT UPDATE ON ALL TABLES` from
0001 is narrowed to a column list that excludes it. After this migration, an
`UPDATE users SET is_system_admin = true` on the application connection fails with a
privilege error whatever code path issues it. That is the property the whole feature rests
on, and it is enforced by Postgres rather than by remembering.

**`zenith_platform`.** Reading across tenants needs RLS bypassed, and the obvious candidate —
the owner connection the CLI uses — can also drop tables. This role bypasses RLS and holds
DML only: no DDL, ever. Created NOLOGIN with no password, exactly like `zenith_app`, so the
credential is the operator's to set and never lives in the repository.

**`tenants.status`.** `suspended` cuts off access without touching a byte of data, and is
reversible. `purging`/`purged` are the one-way door, and the row survives a purge as a
tombstone: the organisation's name stays taken and the fact that it existed stays on record.

The login function is amended in the same breath. Suspending a tenant has to stop new tokens
being issued, and doing it in `zenith_authenticate_lookup` means it holds however many
callers `AuthService` grows later.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Everything the application legitimately updates on a user. Deliberately a list rather
#: than "everything except the flag": a column added later is not writable until somebody
#: adds it here and thinks about whether it should be.
APP_WRITABLE_USER_COLUMNS = "email, name, password_hash, token_version"

#: Same body as 0002 plus the status filter. Replaced rather than wrapped so there is one
#: definition to read — a login path assembled from two layers is one nobody audits.
LOOKUP_FUNCTION = """
CREATE OR REPLACE FUNCTION zenith_authenticate_lookup(p_email text)
RETURNS TABLE (user_id uuid, tenant_id uuid, password_hash text, token_version integer)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT u.id, u.tenant_id, u.password_hash, u.token_version
    FROM users u
    JOIN tenants t ON t.id = u.tenant_id
    WHERE u.email = p_email
      AND t.status = 'active'
$$
"""

ORIGINAL_LOOKUP_FUNCTION = """
CREATE OR REPLACE FUNCTION zenith_authenticate_lookup(p_email text)
RETURNS TABLE (user_id uuid, tenant_id uuid, password_hash text, token_version integer)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT u.id, u.tenant_id, u.password_hash, u.token_version
    FROM users u
    WHERE u.email = p_email
$$
"""


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
    )
    op.add_column(
        "tenants", sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True)
    )
    # `purging` is not in the product's vocabulary but is in the schema's: the purge runs in
    # the background, and without a state for "in flight" the screen cannot tell queued from
    # finished, nor stop somebody starting it twice.
    op.create_check_constraint(
        "ck_tenants_status",
        "tenants",
        "status IN ('active', 'suspended', 'purging', 'purged')",
    )

    op.add_column(
        "users",
        sa.Column("is_system_admin", sa.Boolean(), nullable=False, server_default="false"),
    )

    # The narrowing. `REVOKE UPDATE` then a column list: INSERT and DELETE are untouched,
    # so provisioning still works, and only this one column becomes unwritable.
    op.execute("REVOKE UPDATE ON users FROM zenith_app")
    op.execute(f"GRANT UPDATE ({APP_WRITABLE_USER_COLUMNS}) ON users TO zenith_app")

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenith_platform') THEN
                CREATE ROLE zenith_platform NOLOGIN BYPASSRLS;
            END IF;
        END
        $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO zenith_platform")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO zenith_platform"
    )
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO zenith_platform")

    op.execute(LOOKUP_FUNCTION)


def downgrade() -> None:
    op.execute(ORIGINAL_LOOKUP_FUNCTION)

    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM zenith_platform")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM zenith_platform")
    op.execute("REVOKE USAGE ON SCHEMA public FROM zenith_platform")
    op.execute("DROP ROLE IF EXISTS zenith_platform")

    op.execute("GRANT UPDATE ON users TO zenith_app")
    op.drop_column("users", "is_system_admin")

    op.drop_constraint("ck_tenants_status", "tenants", type_="check")
    op.drop_column("tenants", "status_changed_at")
    op.drop_column("tenants", "status")
