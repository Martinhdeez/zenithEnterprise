"""Lexical search stopped finding Spanish words typed without their accents.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-25

`chunks.tsv` was `to_tsvector('english', text)`, and the query side used the same
configuration — which is the property `lexical.py` exists to protect, and it is still true
after this. What neither side did was fold accents:

    to_tsvector('english', 'máximo') @@ to_tsquery('english', 'maximo')  ->  false

So a reader searching for `detencion`, `articulo` or `codigo` — which is how people type when
they are in a hurry, and how every Spanish keyboard-less phone types — got **nothing at all**
from the lexical half. The dense half still answered, so the symptom was not an error: it was
half a search, quietly, on exactly the corpus this product is being sold into.

## Why not simply switch to `'spanish'`

Measured before assuming, and the measurement said no. On the corpus as it stands:

| | passages |
|---|---|
| English documents | 16,355 |
| Spanish documents | 4,940 |

Seventy-seven per cent of the corpus is English. `'spanish'` would fix the accents and take
English stemming and English stop-words away from three quarters of the passages to do it.
The right answer for a genuinely mixed corpus is per-document language, which needs language
detection and is a larger piece of work than this.

What `'spanish'` *would* also have bought is worth recording, because it is the strongest
evidence yet for that larger piece of work. The same question matched:

    english: 5,273 of 21,295 passages  (24.8%)
    spanish: 1,245 of 21,295 passages  (5.8%)

(Measured on the corpus as it stood that day. Two tenants of M0 leftovers — 13 document rows
each, no files on disk — were removed the following morning, so the installation now holds
8,273 passages and the absolute numbers here cannot be reproduced. The ratio is the finding
and it is unchanged; the figures are left as they were taken, because a migration records what
was measured rather than what is true later.)

a four-fold difference, driven by `de`, `la`, `el` and `los` being content words to an English
analyser. `ts_rank_cd` has no IDF and must score every matching row before taking the top 50,
so the size of that match set *is* the lexical wall this project measured at 5,953 ms on
300,000 passages. Language-aware indexing is not only a quality question; it is four times off
the cost driver. See `.artifacts/todo/2026-08-21-lexical-scalability-bm25.md`.

## What this does instead

A configuration that keeps every English rule and adds accent folding in front of it:
`unaccent` runs as a dictionary before the English stemmer, for every token type that carries
letters. English text is unaffected — it has almost no accents, and the ones it does have are
in loanwords where folding is what a reader wants anyway. Spanish text keeps its (wrong, but
unchanged) English stemming and gains accent insensitivity.

Strictly better, in other words, and deliberately not more than that. The stemming and
stop-word problems above are real and are not fixed here, because fixing them properly means
knowing what language each document is in.

Both sides move together. `lexical.py` reads `CONFIGURATION`, so the index and the query stay
tokenised by the same analyser — the rule that module was written to record after a regex on
the query side once cost lexical search five identifiers out of six.
"""

import sqlalchemy as sa

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

#: Matches `app.features.retrieval.lexical.CONFIGURATION`.
CONFIGURATION = "zenith_text"

#: The token types a Snowball dictionary is mapped over in the stock `english` configuration.
#: Only the ones that can carry letters: `int`, `uint`, `float` and the rest are left alone,
#: because folding accents in a number is meaningless and remapping them risks changing how an
#: identifier like `1545-0074` is stored — which lexical search depends on keeping whole.
LETTER_TOKENS = (
    "asciiword",
    "asciihword",
    "hword_asciipart",
    "word",
    "hword",
    "hword_part",
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.execute(f"DROP TEXT SEARCH CONFIGURATION IF EXISTS {CONFIGURATION}")
    op.execute(f"CREATE TEXT SEARCH CONFIGURATION {CONFIGURATION} (COPY = english)")
    for token in LETTER_TOKENS:
        # `unaccent` first, then the English stemmer. A dictionary list is tried in order and
        # `unaccent` never rejects a token, so it acts as a filter that hands the folded form
        # onward rather than as a lookup that might end the chain.
        op.execute(
            f"ALTER TEXT SEARCH CONFIGURATION {CONFIGURATION} "
            f"ALTER MAPPING FOR {token} WITH unaccent, english_stem"
        )

    # The generated column has to be dropped and rebuilt: a `GENERATED ALWAYS AS` expression
    # cannot be altered in place. The index goes with it and comes back after, which is also
    # the cheaper order — building a GIN index over a column that is about to be recomputed
    # would be paid for twice.
    op.execute("DROP INDEX IF EXISTS ix_chunks_tsv")
    op.drop_column("chunks", "tsv")
    op.add_column(
        "chunks",
        sa.Column(
            "tsv",
            sa.dialects.postgresql.TSVECTOR(),
            sa.Computed(f"to_tsvector('{CONFIGURATION}', text)", persisted=True),
            nullable=False,
        ),
    )
    op.execute("CREATE INDEX ix_chunks_tsv ON chunks USING gin (tsv)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_tsv")
    op.drop_column("chunks", "tsv")
    op.add_column(
        "chunks",
        sa.Column(
            "tsv",
            sa.dialects.postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=False,
        ),
    )
    op.execute("CREATE INDEX ix_chunks_tsv ON chunks USING gin (tsv)")
    op.execute(f"DROP TEXT SEARCH CONFIGURATION IF EXISTS {CONFIGURATION}")
    # `unaccent` is left installed. Dropping an extension another migration or an operator may
    # have come to rely on is a larger claim than this downgrade is entitled to make.
