"""invitation and reset links, instead of passwords somebody has to carry

Revision ID: 0016
Revises: 0015

`POST /users/invite` generates a password, shows it once and never stores it. That was the
right first move — this product ships into networks with no outbound SMTP, so requiring a
mail server to add a colleague would make the feature undeployable exactly where it is
sold — and the invite handler's own docstring names what it costs and what replaces it:

    The cost is written down rather than glossed: a password passed through a chat message
    is a password in a chat log. An invitation token with a set-password page is the proper
    answer and needs a public unauthenticated route and a token table.

This is that table. **Still no email**: the administrator copies a link, exactly as they
copied a password before. What changes is what is in their clipboard.

A password in a chat log works forever and is the user's real credential. A link is single
use and expires, so the same paste is worthless the moment it has been used once — and the
password it produces was chosen by the person whose password it is, and never travelled.

The same mechanism answers the other half: **a forgotten password no longer needs SSH.**
Recovery was `zenith reset-password` on the server, which does not survive a third customer
and which makes every forgotten password an escalation to whoever holds the SSH key.

**Hashed at rest.** The column holds sha256 of the token, never the token. A database dump —
a backup on a laptop, a support export — must not be a set of working links into every
account. The lookup is by hash, so this costs nothing: sha256 and not bcrypt because the
secret is 32 bytes of `secrets.token_urlsafe`, not a human-chosen password, and there is no
dictionary to slow an attacker down against.

**Read through a SECURITY DEFINER function**, like `zenith_authenticate_lookup` in 0002 and
for the same reason: the route that consumes a link is unauthenticated and therefore has no
tenant to bind a session to, so RLS has nothing to filter on. The function is the one narrow
hole, it takes a hash and returns one row, and `REVOKE ALL ... FROM PUBLIC` keeps it that
way.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Takes the hash, never the token. Returns nothing for a token that is expired, already
# used, or was never issued — three different failures that must look identical from
# outside, since telling them apart tells an attacker which guesses were close.
LOOKUP = """
CREATE FUNCTION zenith_credential_token_lookup(p_hash text)
RETURNS TABLE (token_id uuid, user_id uuid, tenant_id uuid, email text, purpose text)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT t.id, t.user_id, t.tenant_id, u.email, t.purpose
    FROM credential_tokens t
    JOIN users u ON u.id = t.user_id
    WHERE t.token_hash = p_hash
      AND t.used_at IS NULL
      AND t.expires_at > now()
$$
"""

# Consuming and setting the password happen in one function for one reason: they must not be
# separable. Two calls means a window where a link has been spent and no password was set,
# and the person is locked out holding a link that no longer works.
#
# `token_version` is bumped in the same statement. For a reset that is the point — if the
# reason for the reset is that somebody else had the account, a new password that leaves
# their existing session alive has fixed nothing.
CONSUME = """
CREATE FUNCTION zenith_credential_token_consume(p_hash text, p_password_hash text)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_user uuid;
BEGIN
    UPDATE credential_tokens
       SET used_at = now()
     WHERE token_hash = p_hash
       AND used_at IS NULL
       AND expires_at > now()
    RETURNING user_id INTO v_user;

    IF v_user IS NULL THEN
        RETURN NULL;
    END IF;

    UPDATE users
       SET password_hash = p_password_hash,
           token_version = token_version + 1
     WHERE id = v_user;

    -- Every other outstanding link for this person dies here. Two invitations issued by two
    -- administrators must not both remain usable once one has been redeemed.
    UPDATE credential_tokens
       SET used_at = now()
     WHERE user_id = v_user AND used_at IS NULL;

    RETURN v_user;
END
$$
"""


def upgrade() -> None:
    op.create_table(
        "credential_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # sha256 of the token. Never the token — see the module docstring.
        sa.Column("token_hash", sa.String(), nullable=False, unique=True),
        # `invitation` or `reset`. The set-password page says different things for each, and
        # the audit trail distinguishes creating an account from recovering one.
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # Set on use. Nullable rather than a boolean, because *when* it was redeemed is the
        # question asked afterwards, and a boolean cannot answer it.
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "purpose IN ('invitation', 'reset')", name="ck_credential_tokens_purpose"
        ),
    )

    op.execute("ALTER TABLE credential_tokens ENABLE ROW LEVEL SECURITY")
    # **Deliberately not FORCE**, and this is the one table in the schema where that is
    # right. FORCE applies the policy to the table's owner as well, which is exactly what
    # defeats a SECURITY DEFINER function: the two functions below run as the owner, the
    # policy still evaluates, `zenith_current_tenant()` is NULL in a session with no tenant
    # bound, and every link reports itself invalid the moment it is issued.
    #
    # `users` is set the same way for the same reason — `zenith_authenticate_lookup` from
    # 0002 could not log anybody in otherwise. ENABLE still applies the policy to
    # `zenith_app`, which is the role every request actually uses.
    # Administrators issuing and listing links work inside their tenant like everything
    # else. Only the two functions above cross this policy, and only by hash.
    op.execute(
        "CREATE POLICY credential_tokens_isolation ON credential_tokens "
        "USING (tenant_id = zenith_current_tenant()) "
        "WITH CHECK (tenant_id = zenith_current_tenant())"
    )

    for statement in (LOOKUP, CONSUME):
        op.execute(statement)

    op.execute("REVOKE ALL ON FUNCTION zenith_credential_token_lookup(text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION zenith_credential_token_lookup(text) TO zenith_app")
    op.execute("REVOKE ALL ON FUNCTION zenith_credential_token_consume(text, text) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION zenith_credential_token_consume(text, text) TO zenith_app"
    )

    # 0001 granted `zenith_app` every table *that existed then*, so a table added later gets
    # nothing unless it is said here. The symptom is not a startup failure — it is inviting
    # a colleague and getting "permission denied" from a table nobody has heard of.
    op.execute("GRANT SELECT, INSERT, UPDATE ON credential_tokens TO zenith_app")

    # 0010 granted `zenith_platform` the tables that existed then; this one is newer, and
    # provisioning an organisation issues its first administrator a link.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON credential_tokens TO zenith_platform")
    op.execute("GRANT EXECUTE ON FUNCTION zenith_credential_token_lookup(text) TO zenith_platform")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS zenith_credential_token_consume(text, text)")
    op.execute("DROP FUNCTION IF EXISTS zenith_credential_token_lookup(text)")
    op.execute("DROP POLICY IF EXISTS credential_tokens_isolation ON credential_tokens")
    op.drop_table("credential_tokens")
