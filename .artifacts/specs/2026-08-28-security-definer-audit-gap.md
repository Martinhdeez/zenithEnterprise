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

## Seven declared, nine installed

The declared count is seven. A running installation at `0022 (head)` was found holding **nine**.
The two extra are `zenith_lexical(query_string text, want integer)` and
`zenith_lexical_real(query_string text, want integer)`: `SECURITY DEFINER`, owned by the schema
owner, carrying `PUBLIC EXECUTE`, declared in no migration, no file and no branch — `git log
--all -S` finds nothing. Their bodies read `FROM bm_test` and `FROM real_chunks`, scratch tables
from the F18 BM25 investigation. Both tables have since been dropped, so either call raises
`relation "bm_test" does not exist` and **the current exposure is nil**. They are not being
dropped here: removing objects from a customer's database is their decision, not ours.

They are the evidence that auditing the declared schema is not sufficient on its own. Nothing
in the repository knew they existed, and nothing in the repository could have: a test built
from the migrations audits what the migrations say, and these were typed into a live database.
Same shape as the trap CLAUDE.md already records — a green suite does not mean the database is
migrated — and the same distinction it draws between `make check` and `demo-check`.

So the audit runs in two places against one list. `AUTHORISED_SECURITY_DEFINERS` lives in
`app/core/diagnostics.py`; `test_security_definer_audit.py` holds the declared schema to it and
`zenith diagnose` holds the installation to it. Two copies would be two catalogues of justified
bypasses drifting apart, which is the failure this document is about.

## What the audit now guarantees

Against the schema the migrations declare (`test_security_definer_audit.py`):

1. The set of `SECURITY DEFINER` functions in `public` equals the allowlist, each entry
   carrying the migration that created it and why the bypass is justified. Equality holds in
   both directions, so a function that disappears invalidates its entry too.
2. Every one of them pins a `search_path`. Without it, owner-privileged code resolves names
   through schemas the caller controls, which is the standard escalation against a
   `SECURITY DEFINER` function. All seven pin one today.
3. Which of them `PUBLIC` may execute is recorded rather than assumed. Four can: the three
   0003 triggers, harmlessly, because Postgres will not let anyone call a `trigger` function
   directly; and `zenith_lexical_search`, because 0022 granted `EXECUTE` to `zenith_app`
   without the `REVOKE ALL ... FROM PUBLIC` that 0002 and 0016 both perform. That is an
   omission rather than a decision, and the fix is a new migration.

Against the installation (`zenith diagnose`, check `bypass surface`):

4. An undeclared function is reported with its owner and whether `PUBLIC` may execute it —
   a failure if it may, a warning if not. A declared function absent from the installation
   fails: at head, that means somebody has been editing a live schema by hand.

The consequence is procedural rather than technical, and it is the point: the next migration
that adds one turns the suite red, and the next function typed into a database turns
`diagnose` red, and in both cases somebody has to write down in the allowlist which migration
created it and what guarantee it buys. ADR 0001's rule — the bypass surface grows for a
security guarantee, never for ergonomics — now has somewhere it is actually recorded.

**Known limitation.** `diagnostics._scrub` removes every known secret by exact match, and the
default database password in `.env.example` is the word `zenith` — the prefix of every
identifier this schema owns. On an installation that kept the default, the check reports
`***_lexical(...)`. The count, the owner and the reach survive; the name does not.

CLAUDE.md invariant 2 and the stale comments in migrations 0002 and 0022 were corrected
alongside.
