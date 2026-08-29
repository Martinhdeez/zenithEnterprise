"""The statements that will not prune when `chunks` is partitioned, held to a list.

ADR 0009 partitions `chunks` and `chunk_embeddings` by `tenant_id`. Every statement carrying
a tenant then prunes to one partition and gets faster; that is the point of the ADR and
`partition-rls-policy-pruning.sql` proves it survives the real policy. **Every statement not
carrying one goes from a single scan to `modulus` scans**, and at the planned modulus of 256
that costs what `backend/eval/unpruned-queries.json` measures: the lexical half of a search
goes from 0.70 ms to 56.8 ms, mostly in *planning*, and a purge goes from 15 locks to 1,552
of an installation's 6,400.

This file is the list, and the list is the point. It exists in the shape of
`test_security_definer_audit.py`, for the same reason: a surface that grows by one entry per
branch, with nobody made to notice, is not audited. When somebody adds a statement that
touches these two tables without a tenant, this test goes red and its author has to write
down which entry it is and why it is acceptable.

## What it asks, and of what

Three questions, of two different sources, because "what the schema declares" and "what the
code writes" are different questions and neither answers the other:

1. **`SECURITY DEFINER` functions**, of the database. These are the third class of bypass —
   no policy applies to them and no Python identifier names them — and two of the seven touch
   `chunks`. Read out of `pg_proc.prosrc`.
2. **Foreign keys touching `chunks` or `chunk_embeddings`**, of the database. A referential
   action is a statement Postgres writes, and it can only carry the partition key if the key
   is in the constraint. Every one of them is on the list today and every one is a stage 02
   decision.
3. **Modules that reach the database through a bypass factory and name these tables**, of the
   source tree. `owner_session` and `platform_session` are greppable by design; this is that
   grep, made into an assertion.

## What it deliberately does not do

It does not parse SQL. Question 1 decides "carries a tenant" by looking for a `tenant_id =`
qualifier in the function body, which is a text test on text — and it is the same test a
reviewer performs by eye. It is stated here rather than implied so that nobody reads a green
run as a proof of pruning. **What proves pruning is `backend/eval/unpruned-queries.json`,
which reads plans.** This file only refuses to let the surface grow unnoticed.

It also runs against the testcontainers fixture, so what it audits is the schema the
migrations *declare*. That is the same limitation `test_security_definer_audit.py` records
about itself, and for the same reason: a hand-made function on a running installation is
invisible here.
"""

import ast
import re
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

#: Marked per test rather than for the module: two of the five are synchronous, and a blanket
#: `pytestmark` puts an `asyncio` mark on them that pytest reports as a warning on every run.
#: A suite that prints warnings nobody reads is a suite whose real warnings go unread.
_asyncio = pytest.mark.asyncio

#: The two tables ADR 0009 partitions. Nothing else on the schema is partitioned, so nothing
#: else can fail to prune.
PARTITIONED = ("chunks", "chunk_embeddings")

#: Where the application lives, relative to this file.
APP = Path(__file__).resolve().parents[2] / "app"


#: Every statement or constraint that will not prune once `chunks` and `chunk_embeddings` are
#: partitioned, and the reason each is on the list.
#:
#: An entry is a decision, not an observation. Adding one means somebody decided the cost is
#: acceptable or that fixing it belongs to another stage, and said which. Removing one means
#: the statement now carries a tenant — which is a change to the code, not to this file.
#:
#: Costs quoted below come from `backend/eval/unpruned-queries.json` at MODULUS 256, and from
#: `backend/eval/unpruned-plpgsql-pruning.sql`. No figure here was typed from memory.
UNPRUNED_SURFACE: frozenset[str] = frozenset(
    {
        # --- SECURITY DEFINER functions ------------------------------------------------
        #
        # 0022 — the lexical half of every search. Its tenant clause is a Tantivy term inside
        # the `@@@` operand, and a `@@@` operand is not a partition-key qualifier, so the
        # planner opens every partition: 256 ParadeDB custom scans, 81.5 ms of planning
        # against 0.25 ms unpartitioned, 1,542 locks. **This is the most expensive entry on
        # the list and the only one on a hot path.**
        #
        # It stays on the list because the fix is a migration and `chunks` is not partitioned
        # yet, so the fix would be dead code today with an Alembic revision number this
        # branch was not assigned. `unpruned-plpgsql-pruning.sql` measures the exact form
        # stage 02 should ship: read `zenith_current_tenant()` into a plpgsql local and put
        # that local on the WHERE clause *beside* the Tantivy term, never instead of it. A
        # tenant *parameter* would be a leak — the function runs as its owner, so its tenant
        # clause is the only thing between one customer and another's passages, and a caller
        # who may pass the tenant may pass somebody else's.
        "secdef:zenith_lexical_search(query_string text, want integer)",
        # 0003 — propagates a document's labels down to its passages:
        # `UPDATE chunks SET label_ids = NEW.label_ids WHERE document_id = NEW.id`, with no
        # tenant at all. Scans 256 partitions and opens 256 for writing; 1,546 locks. Fires
        # once per document whose labels change, and once per document during a purge.
        #
        # On the list for the same reason as above — it is a migration — and the fix is one
        # column: the trigger is on `documents`, so `NEW.tenant_id` is already in hand, and a
        # plpgsql field reference is a parameter, which prunes at plan time.
        "secdef:zenith_sync_chunk_labels()",
        # --- foreign keys ----------------------------------------------------------------
        #
        # Every one of these is a stage 02 decision and none of them is optional, because a
        # partitioned table's unique constraints must contain the partition key: `chunks (id)`
        # becomes `chunks (id, tenant_id)` and every key referencing it becomes composite.
        # They are listed so that the choice is made deliberately rather than discovered when
        # a migration fails to apply.
        #
        # `documents (id)` is the one that is *not* forced and is worth the most. The purge
        # cascade reaches `chunks` through it, so with `tenant_id` in the constraint the
        # referential action carries a tenant: measured, a purge goes from 1,552 locks to 22.
        "fk:chunks.fk_chunks_document_id",
        "fk:chunk_embeddings.fk_chunk_embeddings_chunk_id",
        "fk:query_citations.fk_query_citations_chunk_id",
        # --- modules reaching these tables through a bypass factory -----------------------
        #
        # `zenith diagnose`'s content check counts both partitioned tables through
        # `owner_session` with no tenant — 256 scans and 1,542 locks each. It is on this list
        # rather than removed from it because the module still names the tables: the count is
        # now taken from `pg_class.reltuples`, which scans nothing and takes 7 locks at any
        # modulus, and the exact `count(*)` remains for the three tables that are not
        # partitioned. See `_content`.
        "bypass:core/diagnostics.py",
    }
)


# --- 1. `SECURITY DEFINER` functions ---------------------------------------------------

#: A SQL equality qualifier on the partition key. This is what the planner can prune on;
#: `paradedb.term('tenant_id', ...)` inside a `@@@` operand matches the table name and this
#: pattern does not match it, which is the distinction the whole file turns on.
_TENANT_QUALIFIER = re.compile(r"tenant_id\s*(?:=|IN\b)", re.IGNORECASE)

_TOUCHES = re.compile(r"\b(?:" + "|".join(PARTITIONED) + r")\b")

_FUNCTIONS = """
SELECT p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')' AS signature,
       p.prosrc AS body
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.prosecdef AND n.nspname = 'public'
ORDER BY signature
"""


async def _unpruned_functions(engine: AsyncEngine) -> set[str]:
    async with AsyncSession(engine) as session:
        rows = (await session.execute(text(_FUNCTIONS))).all()
    return {
        f"secdef:{row.signature}"
        for row in rows
        if _TOUCHES.search(row.body) and not _TENANT_QUALIFIER.search(row.body)
    }


@_asyncio
async def test_no_undeclared_security_definer_touches_these_tables_without_a_tenant(
    app_engine: AsyncEngine,
) -> None:
    """A `SECURITY DEFINER` function is the one bypass that no policy and no grep reaches.

    Two of the seven the schema declares touch `chunks` without a tenant qualifier, and both
    are on the list with the migration that should carry the fix. A third would be a new
    unpruned statement on a hot path with nobody having decided that.
    """
    found = await _unpruned_functions(app_engine)
    declared = {entry for entry in UNPRUNED_SURFACE if entry.startswith("secdef:")}

    assert found == declared, (
        f"undeclared: {sorted(found - declared)}; declared but no longer unpruned: "
        f"{sorted(declared - found)}"
    )


# --- 2. Foreign keys ---------------------------------------------------------------------

_FOREIGN_KEYS = """
SELECT t.relname AS child,
       p.relname AS parent,
       c.conname AS constraint_name,
       (SELECT array_agg(a.attname ORDER BY a.attnum)
        FROM unnest(c.conkey) k JOIN pg_attribute a
          ON a.attrelid = c.conrelid AND a.attnum = k) AS child_columns,
       (SELECT array_agg(a.attname ORDER BY a.attnum)
        FROM unnest(c.confkey) k JOIN pg_attribute a
          ON a.attrelid = c.confrelid AND a.attnum = k) AS parent_columns
FROM pg_constraint c
JOIN pg_class t ON t.oid = c.conrelid
JOIN pg_class p ON p.oid = c.confrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE c.contype = 'f' AND n.nspname = 'public'
  AND (t.relname = ANY(:tables) OR p.relname = ANY(:tables))
ORDER BY t.relname, c.conname
"""


async def _foreign_keys_without_the_partition_key(engine: AsyncEngine) -> set[str]:
    """Two different failures, and a key is listed if it has either.

    **A partitioned child** whose key omits `tenant_id` cannot prune the cascade *into* it:
    the `DELETE` Postgres composes has no partition key to prune on. `chunks.document_id` is
    the one that matters, and it is why a purge scoped perfectly to one tenant opens all 256.

    **A partitioned parent** referenced by a key that omits `tenant_id` will not exist at all:
    a partitioned table's unique constraints must contain the partition key, so
    `chunk_embeddings.chunk_id -> chunks (id)` stops being a legal constraint the moment
    `chunks` is partitioned.

    A key from `chunks (tenant_id)` to `tenants (id)` has neither problem and is deliberately
    not flagged: its child column *is* the partition key, so the cascade from a deleted tenant
    prunes, and `tenants` is not partitioned so its own key is fine. The first version of this
    check flagged both of those, on a rule that looked at the parent side unconditionally.
    """
    async with AsyncSession(engine) as session:
        rows = (await session.execute(text(_FOREIGN_KEYS), {"tables": list(PARTITIONED)})).all()
    return {
        f"fk:{row.child}.{row.constraint_name}"
        for row in rows
        if (row.child in PARTITIONED and "tenant_id" not in (row.child_columns or []))
        or (row.parent in PARTITIONED and "tenant_id" not in (row.parent_columns or []))
    }


@_asyncio
async def test_no_undeclared_foreign_key_reaches_these_tables_without_the_partition_key(
    app_engine: AsyncEngine,
) -> None:
    """A cascade is a statement nobody wrote and everybody pays for.

    `ON DELETE CASCADE` becomes a `DELETE` that Postgres composes from the constraint's
    columns. If `tenant_id` is not among them the cascade cannot prune, whatever the statement
    that triggered it looked like — measured, that is 1,552 locks for a purge against 22 with
    the composite key.
    """
    found = await _foreign_keys_without_the_partition_key(app_engine)
    declared = {entry for entry in UNPRUNED_SURFACE if entry.startswith("fk:")}

    assert found == declared, (
        f"undeclared: {sorted(found - declared)}; declared but no longer present: "
        f"{sorted(declared - found)}"
    )


# --- 3. Modules reaching these tables through a bypass factory -----------------------------

#: The factories CLAUDE.md's second invariant names. `unscoped_session` is the fourth and
#: narrower case — login, before a tenant is known — and is included because "before a tenant
#: is known" is exactly the condition that cannot prune.
BYPASS_FACTORIES = (
    "owner_session",
    "platform_session",
    "unscoped_session",
    "get_owner_session_factory",
    "get_platform_session_factory",
)


def _string_literals(tree: ast.Module) -> list[str]:
    """Every string constant in a module that is not a docstring.

    Comments never reach the AST, and docstrings are excluded here, so what is left is the
    text the module can actually send to Postgres. `purge.py` is why this is not a `grep`: it
    names `chunks` in a comment explaining why it does *not* delete from it, and a gate that
    fired on that would be switched off within a week.
    """
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in docstrings
    ]


def _bypass_modules_naming_these_tables() -> set[str]:
    found: set[str] = set()
    for path in sorted(APP.rglob("*.py")):
        if "/tests/" in path.as_posix():
            continue
        source = path.read_text()
        if not any(factory in source for factory in BYPASS_FACTORIES):
            continue
        literals = _string_literals(ast.parse(source, path.name))
        if any(_TOUCHES.search(literal) for literal in literals):
            found.add(f"bypass:{path.relative_to(APP).as_posix()}")
    return found


def test_no_undeclared_module_reaches_these_tables_through_a_bypass_factory() -> None:
    """The grep CLAUDE.md calls a complete audit, turned into an assertion.

    Module granularity rather than statement granularity, deliberately. A file that both
    bypasses RLS and names a partitioned table is a file whose author has to have thought
    about pruning, and that is the question worth forcing — narrowing it to individual
    statements would need a SQL parser and would fail on the first f-string.
    """
    found = _bypass_modules_naming_these_tables()
    declared = {entry for entry in UNPRUNED_SURFACE if entry.startswith("bypass:")}

    assert found == declared, (
        f"undeclared: {sorted(found - declared)}; declared but no longer present: "
        f"{sorted(declared - found)}"
    )


#: A function of exactly the shape this file exists to catch: `SECURITY DEFINER`, so no policy
#: applies to it, naming `chunks` with no tenant qualifier anywhere in its body.
_A_NEW_UNPRUNED_BYPASS = """
CREATE FUNCTION zenith_audit_probe_unpruned() RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$ BEGIN RETURN (SELECT count(*) FROM chunks); END; $$
"""

#: And its twin, which carries one. It must *not* be reported, or the check would flag every
#: correct function and be switched off within a week.
_A_NEW_PRUNED_BYPASS = """
CREATE FUNCTION zenith_audit_probe_pruned(p_tenant uuid) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$ BEGIN RETURN (SELECT count(*) FROM chunks WHERE tenant_id = p_tenant); END; $$
"""


@_asyncio
async def test_the_check_reports_a_new_unpruned_bypass_and_not_a_pruned_one(
    owner_engine: AsyncEngine,
) -> None:
    """A guard that has never failed is not a guard.

    The three assertions above pass today because the schema matches the list, and they would
    pass just as quietly if the derivation were broken and returned nothing. So this builds
    both cases the derivation has to tell apart — a new `SECURITY DEFINER` function that names
    `chunks` without a tenant, and one that names it with a tenant — proves the check reports
    the first and ignores the second, and rolls back.

    `test_partition_rls_guard.py` makes the same argument about the partition guard, and
    `.artifacts/tested/` records why: three measurements this month were indistinguishable
    from experiments that never ran.

    DDL is transactional in Postgres, so the rollback is complete. The owner engine is needed
    because `zenith_app` may not create functions — which is itself part of why this class of
    bypass only ever arrives in a migration.
    """
    async with owner_engine.connect() as conn:
        try:
            await conn.execute(text(_A_NEW_UNPRUNED_BYPASS))
            await conn.execute(text(_A_NEW_PRUNED_BYPASS))

            rows = (await conn.execute(text(_FUNCTIONS))).all()
            reported = {
                f"secdef:{row.signature}"
                for row in rows
                if _TOUCHES.search(row.body) and not _TENANT_QUALIFIER.search(row.body)
            }

            assert "secdef:zenith_audit_probe_unpruned()" in reported, (
                "the check did not report a SECURITY DEFINER function that counts `chunks` "
                "with no tenant — it would not have caught the next one either"
            )
            assert "secdef:zenith_audit_probe_pruned(p_tenant uuid)" not in reported, (
                "the check reported a function that does carry a tenant qualifier; a check "
                "that flags correct code is a check somebody turns off"
            )
        finally:
            await conn.rollback()


def test_the_list_has_no_entries_the_three_checks_cannot_produce() -> None:
    """A list nobody can reach is a list that rots.

    The three assertions above compare against their own prefix, so an entry with a fourth
    prefix would be accepted by all of them and checked by none. That is exactly how the
    `SECURITY DEFINER` paragraph in CLAUDE.md became false and stayed false for nineteen
    migrations.
    """
    prefixes = {entry.split(":", 1)[0] for entry in UNPRUNED_SURFACE}

    assert prefixes <= {"secdef", "fk", "bypass"}, f"unreachable entries: {sorted(prefixes)}"
