"""Query history privacy, enforced by Postgres instead of by application code.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-05

F15 shipped `GET /query/history` with a filter in Python:

    conditions = ["(:everyones OR q.user_id = :user_id)"]

and said so loudly, because it was the one place in this system where application code did
security work. RLS modelled tenant and label; neither expresses "my rows versus my
colleagues'", so a member holding only `query.history.own` would have seen every question
their colleagues asked if that line were ever deleted.

The questions people ask are more revealing than the documents they read — *"what is my
severance?"*, *"can I be dismissed for this?"* — so this is worth a third context variable.

## The design

`zenith.user_id` joins `zenith.tenant_id` and `zenith.label_ids`, and the policy on
`queries` becomes:

    tenant_id = zenith_current_tenant()
    AND (zenith_reads_all_history() OR user_id = zenith_current_user_id())

`zenith.reads_all_history` is a **boolean the session sets from the caller's permissions**,
not a role name and not a permission string parsed in SQL. The application already resolves
permissions; asking Postgres to re-derive authority would put the catalogue in two places
that can disagree.

**Failing closed:** an unset `zenith.user_id` is `NULL`, and `user_id = NULL` is never true.
A session that forgets to bind it sees nothing rather than everything — the same direction
every other policy here fails in.
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

POLICY = (
    "tenant_id = zenith_current_tenant() "
    "AND (zenith_reads_all_history() OR user_id = zenith_current_user_id())"
)


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION zenith_current_user_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT nullif(current_setting('zenith.user_id', true), '')::uuid
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION zenith_reads_all_history() RETURNS boolean
        LANGUAGE sql STABLE AS $$
            -- Defaults to false. A session that never sets it reads only its own history,
            -- which is the safe direction: the failure of an unbound context is less
            -- access, never more.
            SELECT coalesce(
                nullif(current_setting('zenith.reads_all_history', true), '')::boolean,
                false
            )
        $$
        """
    )

    op.execute("DROP POLICY queries_isolation ON queries")
    op.execute(f"CREATE POLICY queries_isolation ON queries USING ({POLICY}) WITH CHECK ({POLICY})")

    # `query_citations` needs nothing: its policy is `EXISTS (SELECT 1 FROM queries ...)`,
    # which applies the parent's policy in turn. The citations of a query somebody may not
    # read become invisible automatically — which is the reason derived policies were
    # written that way in 0001.


def downgrade() -> None:
    op.execute("DROP POLICY queries_isolation ON queries")
    op.execute(
        "CREATE POLICY queries_isolation ON queries "
        "USING (tenant_id = zenith_current_tenant()) "
        "WITH CHECK (tenant_id = zenith_current_tenant())"
    )
    op.execute("DROP FUNCTION IF EXISTS zenith_reads_all_history()")
    op.execute("DROP FUNCTION IF EXISTS zenith_current_user_id()")
