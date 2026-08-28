"""The BM25 index stops being tokenised as English on a corpus that is not.

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-28

0022 built `ix_chunks_bm25` with `en_stem` and wrote down what that would cost:

    The GIN side is `zenith_text` — `english` with **`unaccent` in front of the stemmer**
    (migration 0018), so `maximo` finds `máximo`. The BM25 side is `en_stem`, which
    lowercases and stems and does **not** fold accents. [...] An equivalent one — unaccent in
    front of the stemmer — has to exist before `ZENITH_LEXICAL_ENGINE=bm25` is defensible on
    anything but an English corpus.

## The equivalent does not exist at this version, and that is the finding

pg_search **0.15.26** — the version pinned in `docker-compose.yml` — offers sixteen
tokenisers (`paradedb.tokenizers()`) and **no token filters at all**. There is no
ASCII-folding filter to put in front of a stemmer, and unknown keys in the `text_fields` JSON
are ignored rather than rejected, so asking for one fails silently:

    SELECT * FROM paradedb.tokenize(
      '{"type":"stem","language":"Spanish","ascii_folding":true}'::jsonb, 'Constitución');
    -- constitu     — the flag changed nothing

ASCII folding arrived with the `pdb.*` tokeniser API in **0.19.0**, which replaced the JSON
syntax this schema is written in. It is an upgrade, not a configuration change, and it is not
this migration's to make.

## What does exist, and what it is worth

A Spanish Snowball stemmer: `{"type": "stem", "language": "Spanish"}`. It is not an accent
folder and must not be sold as one — it folds an accent only where its own suffix stripping
happens to remove the accented syllable. From `eval/lexical-engine.json`, `tokeniser_folding`:
over the **2,756** distinct accented word types in the Spanish half of this installation's
corpus, does a word and its unaccented spelling reduce to the same token?

| tokeniser | accented types that survive unaccenting |
|---|---|
| `zenith_text` (GIN, `unaccent`) | 100% by construction |
| `stem`, Spanish | **68.8%** (1,895 / 2,756) |
| `en_stem` | **0.0%** (0 / 2,756) |

`Constitución` and `Constitucion` still do not meet. That third of the vocabulary is the gap
between this migration and the prerequisite 0022 named, and it closes with a version bump
rather than with a tokeniser.

## What it buys, and what it costs, both measured

`en_stem` on a Spanish corpus is not a compromise, it is the wrong analyser: it stems English
suffixes off Spanish words and folds nothing. From `eval/lexical-engine.json`, two runs over
13,549 passages either side of this migration, headline Recall@8 for the `bm25` engine:

| condition | `tsvector` | `bm25`, `en_stem` | `bm25`, Spanish |
|---|---|---|---|
| ten Spanish questions, accented | 1.00 | 1.00 | **1.00** |
| the same, accents stripped | 0.90 | 0.90 | **1.00** |
| `questions.toml`, thirty English | 0.90 | 0.90 | **0.80** |

The accent-stripped row is what this migration is for, and it is the one row where BM25 beats
the engine that ships: `¿Cual es la duracion maxima de la detencion preventiva?` is found,
and `tsvector` loses it.

**The English row is the cost and it is not small.** This installation holds both languages —
Spanish legislation and English standards — and one BM25 index has one analyser for one
field. The two stemmers disagree on **52.9%** of the 10,791 distinct word types in the
English half, so English morphology stops conflating, and two English questions fall out of
the page. There is no per-language analysis to reach for: `text_fields` takes one tokeniser
per column.

**So the tokeniser belongs to the installation's corpus, not to the product**, and this is
the sentence to read before copying this migration onto another one. It is set here for an
installation whose corpus is Spanish-led and whose demonstrations are typed without accents.
An English-led installation should stay on `en_stem`: that is `downgrade()`, which is a
working statement rather than a rollback path nobody expects to take.

Neither choice is lost work while `ZENITH_LEXICAL_ENGINE` is `tsvector`, because the GIN
index is still what answers. The table above is what the switch would cost or buy on the day
somebody flips it, which is the only reason to have measured it in advance.

## Rollout, unchanged from 0022 and repeated because it still applies

`CREATE INDEX` is **not** `CONCURRENTLY`: alembic runs migrations in a transaction and
`CONCURRENTLY` cannot. The index is dropped and rebuilt here, so on an installation large
enough for the lock to matter, do it by hand outside the migration — drop and recreate with
`CONCURRENTLY`, then run this migration against an index that already exists, which is what
`IF NOT EXISTS` is for.

Nothing about the function, the isolation predicates or the custom-scan confinement changes.
`zenith_lexical_search` reads the field through the index's own analyser via `paradedb.match`,
so the tokeniser swap reaches the query side without a line of application code — which is
the property `lexical.py`'s rule exists to protect: *never tokenise a query with anything but
the analyser that built the index.*

`ZENITH_LEXICAL_ENGINE` still defaults to `tsvector`. This is a prerequisite of flipping it,
not the flip.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The indexed columns and the key field are 0022's and are not this migration's business:
# `tenant_id`, `label_ids` and `unlabelled` are in the index because the isolation predicates
# have to be expressible inside the Tantivy query, and moving one out would put the score
# back to NULL. Only the analyser changes.
COLUMNS = "(id, text, tenant_id, label_ids, unlabelled)"

SPANISH = '{"text": {"tokenizer": {"type": "stem", "language": "Spanish", "lowercase": true}}}'
ENGLISH = '{"text": {"tokenizer": {"type": "en_stem", "lowercase": true}}}'


def _rebuild(text_fields: str) -> None:
    """Drop and recreate. A BM25 index's analyser is not alterable in place.

    `DROP` before `CREATE` rather than building beside it and swapping: two BM25 indexes on
    `chunks` at once would double the write amplification of every ingestion running at the
    time, and the window this closes — one statement inside one transaction — is shorter than
    the reindex either way.
    """
    op.execute("DROP INDEX IF EXISTS ix_chunks_bm25")
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS ix_chunks_bm25 ON chunks
          USING bm25 {COLUMNS}
          WITH (key_field = 'id', text_fields = '{text_fields}')
        """
    )


def upgrade() -> None:
    _rebuild(SPANISH)


def downgrade() -> None:
    _rebuild(ENGLISH)
