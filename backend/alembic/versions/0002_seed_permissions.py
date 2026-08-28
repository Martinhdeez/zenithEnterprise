"""seed the permission catalogue and the authentication lookup

Revision ID: 0002
Revises: 0001

Two things live here, both of which have to be versioned rather than done at startup.

The catalogue is seeded by a migration because seeding it on boot makes the row set
depend on which version of the application last started — unversioned state pretending
to be schema. The rows are copied literally rather than imported from
`app.features.auth.access.permissions`: a migration that imports application code stops being
reproducible the moment that code is refactored.

The lookup function exists because login is the one query that cannot have a tenant
context: the context is what the login is trying to establish. See its own comment.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CATALOGUE: list[tuple[str, str]] = [
    ("documents.upload", "Upload documents"),
    ("documents.delete.own", "Delete documents uploaded by oneself"),
    ("documents.delete.any", "Delete any document in the tenant"),
    ("query.execute", "Ask questions of the corpus"),
    ("query.history.own", "Read one's own query history"),
    ("query.history.any", "Read the query history of the whole tenant"),
    ("users.invite", "Invite new users"),
    ("users.manage", "Edit and deactivate users"),
    ("roles.manage", "Create and edit roles"),
    ("labels.manage", "Create and assign access labels"),
    ("llm_config.manage", "Configure the generation connector"),
]


# Login has to find a user before any tenant context exists, which means it cannot go
# through RLS. The alternative would be to open an owner session inside an HTTP
# handler, and the owner connection can read every row in the installation.
#
# This function is the narrow version of that bypass: it returns four fields for one
# email and nothing else. It cannot be coaxed into reading documents. It was the first
# `SECURITY DEFINER` object in the schema and, when this was written, the only one —
# 0003 added three more and the count has grown since, so the audit is not a grep:
# `tests/integration/test_security_definer_audit.py` reads them out of `pg_proc` and
# checks them against a declared list. `search_path` is pinned because a SECURITY
# DEFINER function without it can be hijacked by a caller-controlled schema.
#
# It returns every match, not one: emails are unique per tenant, not per installation.
# Deciding what to do with more than one match is the caller's problem, and
# `AuthService` treats it as a failed login rather than leaking that the address is
# registered twice.
_LOOKUP_FUNCTION = """
CREATE FUNCTION zenith_authenticate_lookup(p_email text)
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
    # Bound, not interpolated. A description containing an apostrophe — "Read one's own
    # query history" — is enough to break a literal INSERT, and string-built SQL in a
    # migration is a habit worth not starting.
    op.get_bind().execute(
        sa.text("INSERT INTO permissions (code, description) VALUES (:code, :description)"),
        [{"code": code, "description": description} for code, description in CATALOGUE],
    )

    op.execute(_LOOKUP_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION zenith_authenticate_lookup(text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION zenith_authenticate_lookup(text) TO zenith_app")

    # The only index on `users` is the composite UNIQUE (tenant_id, email), which
    # cannot serve a lookup by email alone. Without this one, every login is a
    # sequential scan. Addresses are stored already lowercased (see `normalise_email`),
    # so a plain column index is enough and an expression index would be over-clever.
    op.create_index("ix_users_email", "users", ["email"])


def downgrade() -> None:
    op.drop_index("ix_users_email", table_name="users")
    op.execute("DROP FUNCTION zenith_authenticate_lookup(text)")
    op.get_bind().execute(
        sa.text("DELETE FROM permissions WHERE code = ANY(:codes)"),
        {"codes": [code for code, _ in CATALOGUE]},
    )
