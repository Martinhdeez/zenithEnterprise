"""The third class of RLS bypass, made auditable — for the schema the migrations declare.

CLAUDE.md's second invariant used to say that grepping for `owner_session` and
`platform_session` was a complete audit of the bypass surface. It was not, and had not been
since migration 0003: a `SECURITY DEFINER` function executes as its owner, so the policies
are not applied to it either, and neither name appears anywhere near one. Migration 0002's
own comment still claimed its function was the only such object in the schema.

The session factories are auditable by grep because they are Python identifiers. A
`SECURITY DEFINER` function is not — it is a string inside a migration that ran once, and
the thing that has to be audited is the *schema*, not the source that built it. So this file
asks a database instead, and compares the answer against a list somebody had to write.

**Which database is the whole limitation of this file.** It runs against the testcontainers
fixture, built from zero by the migrations, so what it audits is the schema as *declared*.
A function created by hand on a running installation is invisible here, and two such were
found on one. `diagnostics._security_definer_surface` asks the same question of the
installation; the list they both read is `AUTHORISED_SECURITY_DEFINERS`, kept in one place
for the reason recorded beside it.

The point is the failure. When the next migration adds one, this test goes red and its
author has to add an entry to that list saying which migration created it and why the bypass
is justified. That is the audit CLAUDE.md claims to have.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.diagnostics import AUTHORISED_SECURITY_DEFINERS, SecurityDefiner, security_definers

pytestmark = pytest.mark.asyncio


async def _declared_schema(engine: AsyncEngine) -> list[SecurityDefiner]:
    async with AsyncSession(engine) as session:
        return await security_definers(session)


async def test_the_bypass_surface_is_exactly_the_allowlist(app_engine: AsyncEngine) -> None:
    """Set equality, in both directions, on purpose.

    An unlisted function is the leak this exists to catch. A listed function that no longer
    exists is the other half: a list that is quietly wrong is worse than no list, because it
    is the one the next reviewer trusts instead of reading the schema.
    """
    found = {function.signature for function in await _declared_schema(app_engine)}

    assert found == AUTHORISED_SECURITY_DEFINERS, (
        f"undeclared bypass: {sorted(found - AUTHORISED_SECURITY_DEFINERS)}; "
        f"declared but absent: {sorted(AUTHORISED_SECURITY_DEFINERS - found)}"
    )


async def test_every_security_definer_function_pins_a_search_path(
    app_engine: AsyncEngine,
) -> None:
    """A `SECURITY DEFINER` function without a fixed `search_path` runs owner-privileged
    code against schemas the *caller* chooses, which is the standard escalation against one.

    Migration 0002 pinned it for that reason and said so; this is the assertion that keeps
    the reason from being optional for everything added afterwards.
    """
    unpinned = [
        function.signature
        for function in await _declared_schema(app_engine)
        if not function.pins_search_path
    ]

    assert unpinned == [], f"SECURITY DEFINER without a pinned search_path: {unpinned}"


#: Which declared functions `PUBLIC` may execute, recorded rather than assumed.
#:
#: `EXECUTE` to `PUBLIC` is the *default* for a function, so restricting one takes a
#: `REVOKE ALL ... FROM PUBLIC` and leaving it takes nothing. 0002 and 0016 revoke; 0003 and
#: 0022 do not, and the reasons are not the same:
#:
#: - the three 0003 entries return `trigger`, which Postgres will not let anyone `SELECT`
#:   directly, so the grant buys a caller nothing;
#: - `zenith_lexical_search` is an ordinary callable function returning chunk ids, and it is
#:   here because **0022 omitted the `REVOKE`** that 0002 and 0016 both perform. Its own
#:   tenant guard is what actually contains it, not the grant. Not corrected here: a shipped
#:   migration's `upgrade()` is immutable, so the fix is a new migration and a decision
#:   somebody takes deliberately rather than a side effect of writing this test.
#:
#: The set is asserted so that neither state changes without somebody noticing. It is the
#: repository's record of an omission, which is the only reason to write a test that agrees
#: with a thing it does not endorse.
PUBLIC_EXECUTE_TODAY: frozenset[str] = frozenset(
    {
        "zenith_sync_document_labels()",
        "zenith_sync_chunk_labels()",
        "zenith_fill_chunk_labels()",
        "zenith_lexical_search(query_string text, want integer)",
    }
)


async def test_which_declared_functions_public_may_execute_is_unchanged(
    app_engine: AsyncEngine,
) -> None:
    """See `PUBLIC_EXECUTE_TODAY`. Both directions again, for the same reason as above."""
    public = {
        function.signature
        for function in await _declared_schema(app_engine)
        if function.public_execute
    }

    assert public == PUBLIC_EXECUTE_TODAY, (
        f"newly reachable by PUBLIC: {sorted(public - PUBLIC_EXECUTE_TODAY)}; "
        f"no longer reachable: {sorted(PUBLIC_EXECUTE_TODAY - public)}"
    )
