"""The lexical half stops scoring the whole corpus.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-26

`ts_rank_cd` has no IDF and no early termination: to return the best 50 rows it computes a
score for **every** matching row first. A term appearing in a third of a corpus means ranking
a third of the table. Measured on 300,000 passages under the real policy: **5,953 ms** for
the lexical half alone, before the dense half and before reranking.

ParadeDB's BM25 index does something different in kind — `TopNScanExecState` resolves the top
N *inside* the index, so the cost stops being linear in rows matched. That is why the gap is
~130x rather than the ~4x a faster ranking function would buy, and why no hardware closes it.

## Why the isolation columns are in the index

A predicate **inside** the Tantivy query is part of the search. A predicate outside it is a
filter applied to the search's output, and applying one destroys both the scoring and the
plan: `@@@` degrades to a filter, `paradedb.score()` returns NULL, and the scan goes
sequential. ADR 0002 recorded that as "BM25 cannot supply a score under RLS". The true
statement is narrower — BM25 cannot supply a score when the *label clause* is left outside
the query, because `label_ids = '{}' OR label_ids && ...` is not expressible as a pushdown.

So the clause moves inside, and `unlabelled` is materialised because Tantivy expresses "this
field has no values" poorly while the product's rule — a document with no labels is visible
tenant-wide — has to be a first-class clause rather than an absence.

## The function reads the session, and takes no arguments

`zenith_lexical_search` is `SECURITY DEFINER`, which is why the signature matters more than
the body: **there is no parameter through which a caller can ask about another tenant's
corpus.** It reads `zenith.tenant_id` and `zenith.label_ids` through the same
`zenith_current_tenant()` / `zenith_current_labels()` the policies use, so a session with no
context set matches nothing — the same closed failure every policy in this schema has.

It adds one name to the bypass surface. That surface is auditable by grep and now has five
entries: `owner_session`, `platform_session`, and three `SECURITY DEFINER` functions.

## `match`, not `parse`

`lexical.py` states the rule this design had to obey: *never tokenise a query with anything
but the analyser that built the index.* M0 tokenised queries with a regex, which split
`1545-0074` into two tokens while the corpus side had stored it whole, and lexical search
found one identifier in six — a silent failure that nearly cost this project ParadeDB.

`paradedb.parse` would have repeated it in a new form: it takes a query DSL, so the string
would need escaping, and escaping is a second tokeniser by another name. `paradedb.match`
hands the string to the field's own analyser. Two consequences, both wanted:

- The identifier survives. Verified: `1545-0074` matches the row holding it.
- **There is no syntax to inject.** A query string containing
  `contrato) OR tenant_id:(<another tenant>` is treated as terms and returns rows from the
  session's tenant only. Not because it was escaped — because it was never parsed.

## The nested boolean, which is the whole correctness argument

The label alternatives are a `should` **inside** a `must`. `should` alongside `must` is
*optional* in Tantivy — it boosts scoring and does not filter — so the obvious flat
construction returns every row the other clauses match, labels ignored entirely. Nested, it
means "at least one of these, required", which is what the policy says.

Verified against the policy's own SQL predicate on 80,000 rows across two tenants: a session
reaching no labels sees 15,000, one label 45,000, two labels 60,000 — each exact — and zero
rows from the neighbouring tenant.

## The custom scan is confined to the function, and this is not a detail

Putting the isolation columns in the BM25 index is the mechanism — and it also tells the
planner that pg_search's custom scan can serve **any** query filtering on those columns.
On 0.15.26 it then cannot:

```sql
SELECT max(length(text)) FROM chunks WHERE tenant_id = '...';
ERROR:  rt_fetch used out-of-bounds
```

An ordinary column, an ordinary predicate, no `@@@` anywhere — broken by the presence of
the index. It surfaced as a label-sync test failing on a `xmin` read, which is the sort of
symptom nobody traces back to a retrieval change.

So `paradedb.enable_custom_scan` is **off for the database** and switched on by the one
function that needs it, through a per-function `SET`. Every other query in the product is
planned as though pg_search were not installed; the fast path exists in exactly one place,
which is the same shape as the bypass-surface argument above and auditable the same way.

## `en_stem`, and what it is worth

The default Tantivy tokeniser does not stem. `lexical.py`'s rule says both sides must be
tokenised by the same analyser, and the corpus side is `zenith_text` — `english` with
`unaccent` in front of the stemmer. `en_stem` is the closest this index offers, and it is
the principled choice before it is a measured one.

Measured, it moves headline Recall@8 from 80.0% to 85.0% and costs Recall@1 — 56.7% to
46.7%, mean rank 1.40 to 1.65. Shipped anyway, because on this path the cross-encoder
reorders the shortlist: recall into the page is what the lexical half owes, and ordering
within it is somebody else's job.

## What this is worth today: nothing, and that is why it is off by default

Both engines measured against the 30-question set on 13,549 passages:

| engine | headline Recall@8 | Recall@1 | mean rank |
|---|---|---|---|
| `tsvector` | **90.0%** | **66.7%** | **1.41** |
| `bm25`, default tokeniser | 80.0% | 56.7% | 1.40 |
| `bm25`, `en_stem` | 85.0% | 46.7% | 1.65 |

**BM25 is a scale trade, not an upgrade.** At this size `ts_rank_cd` is both fast enough
and more accurate — its proximity component is doing real work that BM25's term statistics
do not replace. What BM25 buys is that its cost does not grow with the number of rows that
match, and at 300,000 passages that is the difference between 5,953 ms and single-digit
milliseconds.

### Before flipping a Spanish corpus: the two indexes do not fold accents the same way

The GIN side is `zenith_text` — `english` with **`unaccent` in front of the stemmer**
(migration 0018), so `maximo` finds `máximo`. The BM25 side is `en_stem`, which lowercases
and stems and does **not** fold accents. Switching engine on a Spanish corpus would
therefore lose accent-insensitive matching silently: no error, no degraded flag, just worse
answers on the half of the queries where somebody typed without accents.

`en_stem` is the closest tokeniser this index offers today. An equivalent one — unaccent in
front of the stemmer — has to exist before `ZENITH_LEXICAL_ENGINE=bm25` is defensible on
anything but an English corpus. That is a prerequisite of the switch, not of this migration,
and it is written here because this is the file somebody will read when they make it.

So `ZENITH_LEXICAL_ENGINE` defaults to `tsvector` and this migration builds a path that
nothing takes yet. That is deliberate: the mechanism is proven, isolated and measured, and
the switch belongs to the installation whose corpus has outgrown the accurate engine — not
to the one being demonstrated next week.

## Rollout

The GIN index stays. Dropping it here would make a rollback a reindex, and the switch itself
is one function call in `lexical()`.

`CREATE INDEX` is **not** `CONCURRENTLY`: alembic runs migrations in a transaction and
`CONCURRENTLY` cannot. On an installation large enough for the lock to matter, build it by
hand outside the migration — the function is written to work against an index that already
exists, and creating it twice is what `IF NOT EXISTS` is for.
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE chunks ADD COLUMN unlabelled boolean
          GENERATED ALWAYS AS (label_ids = '{}'::uuid[]) STORED
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_chunks_bm25 ON chunks
          USING bm25 (id, text, tenant_id, label_ids, unlabelled)
          WITH (key_field = 'id',
                text_fields = '{"text": {"tokenizer": {"type": "en_stem",
                                                       "lowercase": true}}}')
        """
    )
    # Off for the database, on for the one function below. `current_database()` cannot be
    # interpolated into `ALTER DATABASE` directly, hence the format.
    op.execute(
        """
        DO $do$ BEGIN
            EXECUTE format(
                'ALTER DATABASE %I SET paradedb.enable_custom_scan = off',
                current_database());
        END $do$
        """
    )
    op.execute(
        """
        CREATE FUNCTION zenith_lexical_search(query_string text, want integer)
        RETURNS TABLE(chunk_id uuid, score real)
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = public, paradedb
        -- The one place the custom scan is allowed. Off everywhere else; see above.
        SET paradedb.enable_custom_scan = on
        AS $$
        BEGIN
            -- No context, no rows. Deliberately the same closed failure every policy in
            -- this schema has, rather than an error: the argument for this function is
            -- that it expresses the policy's rule in a form the index can use, and a
            -- different failure mode would undermine exactly that claim.
            --
            -- Without this guard `paradedb.term` raises `no value provided to term query`
            -- on a NULL tenant, which is also closed but is not what a policy does.
            IF zenith_current_tenant() IS NULL THEN
                RETURN;
            END IF;

            RETURN QUERY
            SELECT c.id, paradedb.score(c.id)
            FROM chunks c
            WHERE c.id @@@ paradedb.boolean(
                must => ARRAY[
                    -- `match`, not `parse`: the field's own analyser tokenises the string,
                    -- so identifiers survive and there is no query syntax to inject.
                    paradedb.match('text', query_string),
                    paradedb.term('tenant_id', zenith_current_tenant()),
                    -- Nested on purpose: a `should` beside a `must` is optional in Tantivy
                    -- and would ignore labels entirely. Inside a `must` it means "at least
                    -- one of these", which is what the policy says.
                    paradedb.boolean(should =>
                        ARRAY[paradedb.term('unlabelled', true)]
                        || (SELECT coalesce(
                                array_agg(paradedb.term('label_ids', label)),
                                ARRAY[]::paradedb.searchqueryinput[])
                            FROM unnest(zenith_current_labels()) AS label))
                ])
            ORDER BY paradedb.score(c.id) DESC
            LIMIT want;
        END;
        $$
        """
    )
    # The application role calls it; nothing else needs to.
    op.execute("GRANT EXECUTE ON FUNCTION zenith_lexical_search(text, integer) TO zenith_app")


def downgrade() -> None:
    op.execute(
        """
        DO $do$ BEGIN
            EXECUTE format(
                'ALTER DATABASE %I RESET paradedb.enable_custom_scan', current_database());
        END $do$
        """
    )
    op.execute("DROP FUNCTION IF EXISTS zenith_lexical_search(text, integer)")
    op.execute("DROP INDEX IF EXISTS ix_chunks_bm25")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS unlabelled")
