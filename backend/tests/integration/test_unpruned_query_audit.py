"""The statements that will not prune now that `chunks` is partitioned, held to a list.

ADR 0009 partitions `chunks` and `chunk_embeddings` by `tenant_id`, and migration 0026 is
where it happened. Every statement carrying a tenant prunes to one partition and gets
faster; that is the point of the ADR and `partition-rls-policy-pruning.sql` proves it
survives the real policy. **Every statement not carrying one goes from a single scan to
`modulus` scans**, and at the MODULUS 256 the measurements were taken at that costs what
`backend/eval/unpruned-queries.json` records: the lexical half of a search goes from
0.70 ms to 56.8 ms, mostly in *planning*, and a purge goes from 15 locks to 1,552 of an
installation's 6,400. The modulus is `ZENITH_PARTITION_MODULUS` now and defaults to 128, so
every cost quoted here is an upper bound on the shipped default — the locks are linear in it
and the planning is worse than linear. What is not proportional to anything is the list
itself: a statement that does not prune scans `modulus` partitions whatever the modulus is.

This file is the list, and the list is the point. It exists in the shape of
`test_security_definer_audit.py`, for the same reason: a surface that grows by one entry per
branch, with nobody made to notice, is not audited. When somebody adds a statement that
touches these two tables without a tenant, this test goes red and its author has to write
down which entry it is and why it is acceptable.

## What it asks, and of what

Three questions, of two different sources, because "what the schema declares" and "what the
code writes" are different questions and neither answers the other:

1. **`SECURITY DEFINER` functions**, of the database. These are the third class of bypass —
   no policy applies to them and no Python identifier names them — and two of the seven name
   `chunks`. Both carry a tenant qualifier since 0026, so neither is on the list. Read out of
   `pg_proc.prosrc`.
2. **Foreign keys touching `chunks` or `chunk_embeddings`**, of the database. A referential
   action is a statement Postgres writes, and it can only carry the partition key if the key
   is in the constraint. Every one of them is composite since 0026, so none is on the list.
3. **Modules that reach the database through a bypass factory and name these tables**, of the
   source tree. `owner_session` and `platform_session` are greppable by design; this is that
   grep, made into an assertion.

## What came off the list, and how

The list was written before 0026 with five entries on it that 0026 was expected to retire,
each naming the fix it was waiting for. It shipped all five, so all five are gone and this
is the record of which mechanism removed each — because a set difference cannot tell a
statement that was *rewritten* from one that was *dropped*, and only one of those is a fix.
Checked against the schema the migrations build rather than inferred from the arithmetic:

- `zenith_lexical_search(query_string text, want integer)` — **rewritten in place**, still
  present, same identity signature, still `SECURITY DEFINER`, and 0023's revoke from
  `PUBLIC` survived the `CREATE OR REPLACE` — `proacl` names the owner and `zenith_app` and
  carries no `PUBLIC` entry, which is what 0023 exists to be true.
  It reads `zenith_current_tenant()` into a plpgsql local and puts that local on the `WHERE`
  clause *beside* the Tantivy term, which is the form `unpruned-plpgsql-pruning.sql`
  measured and not the redundant `zenith_current_tenant()` qualifier that scored
  `Subplans Removed: 255` and moved no locks.
- `zenith_sync_chunk_labels()` — **rewritten in place**, same signature, same trigger on
  `documents`; the `UPDATE` gained `AND tenant_id = NEW.tenant_id`.
- `fk_chunks_document_id`, `fk_chunk_embeddings_chunk_id`, `fk_query_citations_chunk_id` —
  **made composite**, not dropped. All three are still `ON DELETE CASCADE` and all three now
  read `(…, tenant_id) -> (…, tenant_id)`. 0026's own docstring argues at length against the
  other way of making these stop being reported, which is to drop them.

Both function bodies were read back out of `pg_proc.prosrc` and the qualifier confirmed to be
on an executable line rather than in one of the comments 0026 wrote beside it. That is the
distinction `_TENANT_QUALIFIER` cannot draw on its own, and it is why this paragraph exists
instead of a commit message nobody will find.

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
        # None. `zenith_lexical_search` and `zenith_sync_chunk_labels` were both here until
        # 0026 rewrote them in place; the module docstring records which mechanism retired
        # each and what was checked before the entry was removed. An empty section rather
        # than a deleted one, because the next `SECURITY DEFINER` function to name `chunks`
        # belongs here and its author should find the heading.
        #
        # --- foreign keys ----------------------------------------------------------------
        #
        # None either, and for two different reasons that are worth keeping apart.
        #
        # `fk_chunk_embeddings_chunk_id` and `fk_query_citations_chunk_id` *had* to become
        # composite: a partitioned table's unique constraints must contain the partition key,
        # so `chunks (id)` became `chunks (id, tenant_id)` and every key referencing it
        # became composite or stopped being a legal constraint. Postgres now enforces that
        # half, which is why `_foreign_keys_without_the_partition_key`'s parent-side rule can
        # no longer fire against `chunks` — it is kept anyway, because 0026 has a working
        # `downgrade` and the rule is what would catch the schema on the way back.
        #
        # `fk_chunks_document_id` is the one that was *not* forced and is worth the most. The
        # purge cascade reaches `chunks` through it, so with `tenant_id` in the constraint the
        # referential action carries a tenant: measured, a purge goes from 1,552 locks to 22.
        # Nothing in Postgres would have complained had 0026 left it simple, so nothing but
        # this list and that measurement was ever going to make it happen.
        #
        # --- modules reaching these tables through a bypass factory -----------------------
        #
        # `zenith diagnose`'s content check counts both partitioned tables through
        # `owner_session` with no tenant — one scan per partition and 1,542 locks each at the
        # MODULUS 256 that was measured. It is on this list
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

    Two of the seven the schema declares name `chunks`, and since 0026 both carry a tenant
    qualifier, so the list is empty on this side. That makes the assertion strictly stronger
    than it was: any `SECURITY DEFINER` function reaching a partitioned table without a
    tenant is now a new unpruned statement on a hot path with nobody having decided that.

    It is an equality rather than a subset check in both directions on purpose. `found`
    growing is an undeclared bypass; `declared` growing without `found` is an entry somebody
    added for a statement that does not exist, which is the list rotting the other way.
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
    the one that matters, and it is why a purge scoped perfectly to one tenant opens every
    partition.

    **A partitioned parent** referenced by a key that omits `tenant_id` will not exist at all:
    a partitioned table's unique constraints must contain the partition key, so
    `chunk_embeddings.chunk_id -> chunks (id)` stops being a legal constraint the moment
    `chunks` is partitioned.

    A key from `chunks (tenant_id)` to `tenants (id)` has neither problem and is deliberately
    not flagged: its child column *is* the partition key, so the cascade from a deleted tenant
    prunes, and `tenants` is not partitioned so its own key is fine. The first version of this
    check flagged both of those, on a rule that looked at the parent side unconditionally.

    Since 0026 the query returns `2P + 5` rows rather than five — 261 at the default modulus
    — and a reader checking this in `psql` should know why before concluding the check has
    stopped looking. Postgres clones a foreign key across a partitioned table on both sides:
    `P` rows are `fk_chunk_embeddings_chunk_id` repeated on each `chunk_embeddings_pNNN`, and
    `P` more are the same constraint repeated once per `chunks_pNNN` under a generated name.
    Every clone carries the columns of the constraint it came from, so none is flagged. Five
    rows are
    constraints somebody actually wrote — `fk_chunks_document_id`, `fk_chunks_tenant_id`,
    `fk_chunk_embeddings_chunk_id`, `fk_chunk_embeddings_tenant_id` and
    `fk_query_citations_chunk_id` — and those are the five to look at.

    **The clones are also why the reported findings stay readable, and where the check's reach
    ends.** A bad key added to `chunks` reports once, not once per partition — measured, by
    adding one
    and reading the failure — because its clones sit on `chunks_pNNN`, and a clone's child is
    a partition whose name is in neither `PARTITIONED` nor the parent position. The same fact
    is the limitation: a key added *directly to one partition*, `ALTER TABLE chunks_p017 ADD
    CONSTRAINT ...`, is invisible here. Nothing in this repository writes one — Alembic
    operates on the parent and Postgres propagates — but it is a hole in a fence and it is
    written down rather than left to be discovered, which is the same reason the module
    docstring says this file does not parse SQL.
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
