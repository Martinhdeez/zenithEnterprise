"""Creating a partition, with its row-level security, as one operation.

A partition does not inherit its parent's RLS. `ENABLE ROW LEVEL SECURITY` on the parent
does not reach it, the parent's policy does not apply to it, and a query addressing the
partition directly returns every row it holds. `eval/partition-rls.sql` demonstrates it
against the live database: parent `chunks` reports `rls_enabled = t` with one policy, its
partition `p2` reports `f` with none, and with the context set to tenant 1 a `SELECT` from
`p2` returns tenant 3's ten thousand rows in full.

Reaching those rows still needs a `GRANT` on the partition, and nothing grants one today.
`GRANT ... ON ALL TABLES IN SCHEMA public TO zenith_app` would — migration 0001 issues
exactly that statement — and so would any migration written by somebody who assumed
inheritance. That is invariant 1 broken by a schema change, and it fails in the direction
this repository refuses: the ordinary path through the parent keeps returning the right
rows, so nothing surfaces.

So a partition is never created on its own. `create_partition` emits the `CREATE TABLE`,
the `ENABLE ROW LEVEL SECURITY` and the `CREATE POLICY` together, inside the migration's
own transaction. DDL is transactional in Postgres, so a failure at the policy takes the
table with it: there is no state in which the partition exists and the policy does not.

**This is an Alembic helper and not a SQL function, deliberately.** A SQL function doing
this would have to be created by a migration and would then live in the schema forever, as
a string nobody greps — which is precisely the property that made `SECURITY DEFINER`
functions the unauditable third class of bypass in CLAUDE.md's second invariant. A
`SECURITY DEFINER` one would be worse still: it would widen the very surface
`tests/integration/test_security_definer_audit.py` enumerates and `zenith diagnose` checks,
to save a migration author three lines. A Python identifier is greppable, is type-checked,
and is what the failure mode actually calls for, because the author who assumes inheritance
is writing Python.

Nothing here enforces its own use. `CREATE TABLE ... PARTITION OF` by hand still compiles,
and no helper can prevent that. `tests/integration/test_partition_rls_guard.py` is what
catches it.
"""

from alembic import op

#: The label-level policy, level 2 of ADR 0001 — the row's tenant and then its labels.
#:
#: Copied character for character from CLAUDE.md's first invariant and from migration
#: 0001's `LABEL_POLICIES`, because a partition carrying a policy that is *nearly* the
#: parent's is a compartment leak that reads like a formatting difference in review.
#: `chunks` is the table this applies to.
LABEL_POLICY = (
    "tenant_id = zenith_current_tenant() "
    "AND (label_ids = '{}' OR label_ids && zenith_current_labels())"
)

#: Level 1: the row carries its own `tenant_id` and nothing else. Migration 0001's
#: `TENANT_POLICIES` entry for `chunk_embeddings`, which has no `label_ids` column — an
#: embedding is reachable only through the chunk it belongs to.
TENANT_POLICY = "tenant_id = zenith_current_tenant()"


def partition_statements(
    parent: str, partition: str, tenant_id: str, policy: str = LABEL_POLICY
) -> list[str]:
    """The three statements that make a whole partition, in the order they must run.

    Separate from `create_partition` so the guard test can execute them against a schema of
    its own and prove they satisfy the check. A helper asserted only through the migration
    that calls it is a helper nobody has run against the thing it claims to prevent.
    """
    # A policy name is not schema-qualified, so the schema comes off it. Migrations pass
    # bare names and never reach this; the guard test builds its partitions in a schema of
    # its own, and without the strip its `CREATE POLICY` is a syntax error rather than a
    # result.
    policy_name = partition.rsplit(".", 1)[-1] + "_isolation"
    return [
        f"CREATE TABLE {partition} PARTITION OF {parent} FOR VALUES IN ('{tenant_id}')",
        f"ALTER TABLE {partition} ENABLE ROW LEVEL SECURITY",
        f"CREATE POLICY {policy_name} ON {partition} USING ({policy}) WITH CHECK ({policy})",
    ]


def create_partition(
    parent: str, partition: str, tenant_id: str, policy: str = LABEL_POLICY
) -> None:
    """Create one list partition of `parent` holding `tenant_id`, with its own policy.

    The policy is named `<partition>_isolation`, the convention migration 0001 uses for
    every table it protects, so the guard test's report and the schema read the same way.

    No table is partitioned yet, so this has no caller. It exists ahead of one because the
    stage that adds the first caller is the stage that would otherwise write the three
    statements by hand and stop after the first.
    """
    for statement in partition_statements(parent, partition, tenant_id, policy):
        op.execute(statement)
