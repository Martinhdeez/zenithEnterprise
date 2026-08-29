"""The BM25 function was granted to everybody, because nothing took it away.

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-28

Postgres grants `EXECUTE` on every new function to `PUBLIC`. 0002 and 0016 both know this —
each of their `SECURITY DEFINER` functions is created, revoked from `PUBLIC`, and then
granted to the one role that needs it. 0022 created `zenith_lexical_search` the same way and
issued the grant without the revoke, so the default stood:

    SELECT has_function_privilege('public', p.oid, 'EXECUTE')
    FROM pg_proc p WHERE p.proname = 'zenith_lexical_search';
    -- t

    proacl: {=X/zenith,zenith=X/zenith,zenith_app=X/zenith}

The leading entry with an empty grantee **is** `PUBLIC`. It is an omission against an
established pattern rather than a decision, which is why the fix is one statement and why it
is worth a migration of its own.

## Why the guard inside the function is not the layer that should contain this

`zenith_lexical_search` is `SECURITY DEFINER`, so it runs as the schema owner and reads its
tenant and labels from `zenith.tenant_id` and `zenith.label_ids` — GUCs in the `zenith`
namespace, which **any** role may `set_config`. The function's own argument for safety is
that it takes no tenant parameter, and that argument is about the *caller's* session rather
than about who the caller is: a role that can call it can also choose the session it is
called in. So any role with LOGIN could set a tenant of its choosing and read chunk ids and
BM25 scores for that tenant, holding no grant on `chunks` at all.

Live exposure today is nil, and that is a fact about the current role list rather than about
the design: the only roles that can log in — `zenith`, `zenith_app`, `zenith_platform` —
already read `chunks` directly. The exposure is to the next role somebody creates, which is
exactly the class of bug a `GRANT` is meant to make impossible to reintroduce by accident.

The guard inside the function stays where it is. It answers a different question — *what does
a session with no context see* — and its answer, nothing, has to remain the same closed
failure every policy in this schema has. Containment of *who may ask at all* belongs in the
grant, one layer out, where it is auditable by reading `proacl` instead of by reading plpgsql.

## What this does not change

Nothing in the product calls this function as any role but `zenith_app`, whose grant 0022
already issued and this migration leaves alone. `ZENITH_LEXICAL_ENGINE` still defaults to
`tsvector`; this is a prerequisite of flipping it, not the flip.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTION = "zenith_lexical_search(text, integer)"


def upgrade() -> None:
    # `REVOKE ALL` rather than `REVOKE EXECUTE`: `EXECUTE` is the only privilege a function
    # has, and `ALL` is what 0002 and 0016 say. Matching them keeps the audit a single grep.
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC")


def downgrade() -> None:
    # Restores the state 0022 left, which is the default Postgres would have applied on its
    # own. A downgrade puts the database back where it was; it does not get to be an opinion
    # about whether where it was is a good place.
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO PUBLIC")
