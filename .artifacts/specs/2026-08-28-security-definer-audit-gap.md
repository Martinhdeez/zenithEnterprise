# The `SECURITY DEFINER` audit gap, and why it blocks `ZENITH_LEXICAL_ENGINE=bm25`

**Status:** closed by `backend/tests/integration/test_security_definer_audit.py`.

## The finding

CLAUDE.md's second invariant said that grepping for `owner_session` and `platform_session`
was a complete audit of the RLS bypass surface. It was not, and had not been since migration
0003.

A `SECURITY DEFINER` function executes as its owner. Policies are not applied to it, exactly
as they are not applied to the owner and platform connections — so it is a bypass of the same
kind, and it is the one nobody enumerated. There are seven of them, created by migrations
0002, 0003, 0016 and 0022, and none is reachable by either grep: they are strings inside
migrations that ran once, and the thing that needs auditing is the schema, not the source
that built it.

Migration 0002's own comment still described its function as "the only `SECURITY DEFINER`
object in the schema, so auditing the bypass surface is one grep". True when written, stale
from the next migration onwards, and left standing for twenty more. That is the actual
failure mode: not a leak, but a list that is quietly wrong and is therefore the thing the
next reviewer trusts instead of reading the schema.

## Why it blocks the flip

`ZENITH_LEXICAL_ENGINE` is `tsvector` today. Under `tsvector` the lexical half is an ordinary
query against `chunks`, and its isolation comes from the policy on that table — declarative,
uniform, and identical to the isolation of everything else in the product.

Setting it to `bm25` moves the lexical half onto `zenith_lexical_search`, which is
`SECURITY DEFINER` and by design: the tenant and label clauses have to sit *inside* the
Tantivy query, because a predicate outside it is a filter on the search's output and destroys
both the score and the plan. So under `bm25` the isolation of half of retrieval stops being a
policy Postgres applies and becomes plpgsql that calls `zenith_current_tenant()` and
`zenith_current_labels()` and gets the boolean structure right. `test_bm25_isolation.py`
already holds that function to the same matrix the policies are held to, and that test is
what makes the design defensible.

What was missing is the layer above it. Imperative isolation is acceptable when it is
*visible*: when the documented audit procedure lists the function, so the next person to
touch retrieval knows they are editing a security control rather than a query. The documented
procedure did not inspect that path at all. Flipping the engine would therefore have made an
unaudited class of bypass load-bearing for the product's core guarantee — while the file
describing the guarantee said the audit was complete.

The flip is not blocked on correctness. It is blocked on the audit being true when it happens.

## What the test now guarantees

`test_security_definer_audit.py` queries `pg_proc.prosecdef` in the live schema and asserts:

1. The set of `SECURITY DEFINER` functions in `public` equals an allowlist declared in the
   test file, each entry carrying the migration that created it and why the bypass is
   justified. Equality holds in both directions, so a function that disappears invalidates
   its entry too.
2. Every one of them pins a `search_path`. Without it, owner-privileged code resolves names
   through schemas the caller controls, which is the standard escalation against a
   `SECURITY DEFINER` function. All seven pin one today.

The consequence is procedural rather than technical, and it is the point: the next migration
that adds one turns the suite red, and its author has to write down in the allowlist which
migration created it and what guarantee it buys. ADR 0001's rule — the bypass surface grows
for a security guarantee, never for ergonomics — now has somewhere it is actually recorded.

CLAUDE.md invariant 2 and migration 0002's comment were corrected in the same commit.
