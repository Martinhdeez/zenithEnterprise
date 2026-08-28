"""The third class of RLS bypass, made auditable.

CLAUDE.md's second invariant used to say that grepping for `owner_session` and
`platform_session` was a complete audit of the bypass surface. It was not, and had not been
since migration 0003: a `SECURITY DEFINER` function executes as its owner, so the policies
are not applied to it either, and neither name appears anywhere near one. Migration 0002's
own comment still claimed its function was the only such object in the schema.

The session factories are auditable by grep because they are Python identifiers. A
`SECURITY DEFINER` function is not — it is a string inside a migration that ran once, and
the thing that has to be audited is the *schema*, not the source that built it. So this file
asks the database instead, and compares the answer against a list somebody had to write.

The point is the failure. When the next migration adds one, this test goes red and its
author has to add an entry here saying which migration created it and why the bypass is
justified. That is the audit CLAUDE.md claims to have.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.asyncio

#: Every `SECURITY DEFINER` function the schema is allowed to contain, by identity
#: signature. An overload is a different function and needs its own entry.
#:
#: A bypass belongs here for a security guarantee, never for ergonomics — see ADR 0001, and
#: see `.artifacts/todo/2026-08-02-f5-ingestion.md` for a route that was declined on exactly
#: that rule.
AUTHORISED: frozenset[str] = frozenset(
    {
        # 0002, replaced in place by 0010 — login has to find a user before a tenant context
        # exists, because the context is what the login is establishing. Returns four fields
        # for one address; the alternative was an owner session in an unauthenticated route.
        "zenith_authenticate_lookup(p_email text)",
        # 0003 — maintains `documents.label_ids` from `document_labels`. Bypasses so that an
        # administrator with `labels.manage` can remove a label they do not personally reach
        # without the `WITH CHECK` on `documents` rejecting a row they never mentioned.
        "zenith_sync_document_labels()",
        # 0003 — propagates that same array down to `chunks`, for the same reason.
        "zenith_sync_chunk_labels()",
        # 0003 — gives a chunk its document's labels at insert time; without it a chunk is
        # born unlabelled, which in this schema means readable by the whole tenant.
        "zenith_fill_chunk_labels()",
        # 0016 — an invitation or reset link is consumed by an unauthenticated route, so
        # there is no tenant for a policy to filter on. Takes a hash, returns one row.
        "zenith_credential_token_lookup(p_hash text)",
        # 0016 — spends the link and sets the password in one statement, so there is no
        # window in which the link is used and no password was set.
        "zenith_credential_token_consume(p_hash text, p_password_hash text)",
        # 0022 — BM25 needs the tenant and label clauses *inside* the Tantivy query, which a
        # policy cannot express. Enforces them imperatively instead; `test_bm25_isolation.py`
        # is what makes that enforcement worth the same as a policy.
        "zenith_lexical_search(query_string text, want integer)",
    }
)

#: `public` is the only schema this project creates objects in. The ParadeDB image ships
#: several others — `paradedb`, `topology`, `tiger` — and they are not ours to vet.
#:
#: Extension-owned functions inside `public` are deliberately *not* excluded. Today none of
#: them is `SECURITY DEFINER`, and the day an extension is added that ships one, adding that
#: extension has widened the bypass surface and should be argued for here like anything else.
OWNED_SCHEMA = "public"

_DEFINERS = """
SELECT p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')' AS signature,
       p.proconfig
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.prosecdef AND n.nspname = :schema
ORDER BY signature
"""


async def _security_definers(engine: AsyncEngine) -> dict[str, list[str]]:
    """Signature -> the settings pinned on the function, read from the live catalogue."""
    async with engine.connect() as conn:
        rows = await conn.execute(text(_DEFINERS), {"schema": OWNED_SCHEMA})
        return {signature: list(config or []) for signature, config in rows}


async def test_the_bypass_surface_is_exactly_the_allowlist(app_engine: AsyncEngine) -> None:
    """Set equality, in both directions, on purpose.

    An unlisted function is the leak this exists to catch. A listed function that no longer
    exists is the other half: a list that is quietly wrong is worse than no list, because it
    is the one the next reviewer trusts instead of reading the schema.
    """
    found = set(await _security_definers(app_engine))

    assert found == AUTHORISED, (
        f"undeclared bypass: {sorted(found - AUTHORISED)}; "
        f"declared but absent: {sorted(AUTHORISED - found)}"
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
        signature
        for signature, config in (await _security_definers(app_engine)).items()
        if not any(setting.startswith("search_path=") for setting in config)
    ]

    assert unpinned == [], f"SECURITY DEFINER without a pinned search_path: {unpinned}"
