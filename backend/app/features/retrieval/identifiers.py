"""The third signal: exact identifiers, which ranking by frequency cannot find.

F9 left the context ceiling at 90.3%, and two identifier questions accounted for most of
the gap. F15 measured why, and it is not tokenisation — `to_tsvector` stores `10000w` and
`u.s.c` whole, and the query side produces the same lexemes. The index has the right
terms.

**`ts_rank_cd` has no IDF.** It ranks by term frequency and proximity, so a rare, decisive
identifier scores no better per occurrence than a ubiquitous word. Measured on the real
corpus, for *"What is Catalog Number 10000W?"*:

    rank  1-51   chunks matching "catalog" or "number", none containing the identifier
    rank  52     the chunk that actually contains 10000W        ← just outside CANDIDATES

The passage was found, ranked, and then buried by two common words in the same question.
That is the same shape of failure F7 fixed in fusion — a decisive single signal outvoted by
agreement between weak ones — one layer further down.

**The fix is a second lexical query that ANDs only the identifier-like lexemes.** `10000w`
alone puts the right chunk at rank 1; `23 & 101 & u.s.c` puts *"23 U.S.C. 101"* at rank 4,
from nowhere. It is one extra indexed query, it runs only when the question contains
something identifier-shaped, and its results join the candidate union rather than replacing
anything.

Deliberately **not** BM25. ParadeDB ships `pg_search` and switching the lexical half to it
would fix this class properly — the `score_bm25` column is named for a plan that was not
taken. That is a larger change with its own recall risk across all 36 questions, and it
deserves its own measurement rather than being smuggled in as a bug fix. Recorded in the
F15 write-up as the principled successor to this.
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.retrieval.lexical import CONFIGURATION

# How many exact matches to admit. Small on purpose: this query is precise by construction,
# so a long tail would only add noise to a union the reranker then has to read.
EXACT_CANDIDATES = 10


def identifier_like(lexeme: str) -> bool:
    """Whether a lexeme is an identifier rather than a word.

    Two markers, both measured against the failing cases:

    - **a digit** — `10000w`, `1545-0074`, `23`, `101`
    - **an internal full stop** — `u.s.c`, which carries no digit and is exactly the token
      that moved the U.S.C. citation from unfound to rank 4

    A word never has either. The test is deliberately cheap and slightly generous: a false
    positive costs one extra AND term on a query that is discarded if it matches nothing,
    while a false negative is a question that stays unanswerable.
    """
    return any(character.isdigit() for character in lexeme) or "." in lexeme


async def exact(
    session: AsyncSession, question: str, limit: int = EXACT_CANDIDATES
) -> list[tuple[UUID, float]]:
    """Passages containing *every* identifier in the question.

    AND rather than OR, which is the opposite of `lexical()` and the entire point. `lexical`
    ORs because a question is not a filter; here the identifiers *are* the filter — someone
    asking about `23 U.S.C. 101` wants the passage with all three parts, not the thousand
    passages containing `23`.

    Returns an empty list when the question holds no identifiers, which is most questions.
    The caller pays nothing for them.
    """
    raw = await session.scalar(
        text("SELECT tsvector_to_array(to_tsvector(:config, :question))"),
        {"config": CONFIGURATION, "question": question},
    )
    lexemes: list[str] = list(raw or [])
    identifiers = [lexeme for lexeme in lexemes if identifier_like(lexeme)]
    if not identifiers:
        return []

    query = " & ".join(identifiers)
    rows = await session.execute(
        text(
            "SELECT c.id, ts_rank_cd(c.tsv, q) AS score FROM chunks c, "
            "to_tsquery(:config, :query) q "
            "WHERE c.tsv @@ q ORDER BY score DESC, c.id LIMIT :limit"
        ),
        {"config": CONFIGURATION, "query": query, "limit": limit},
    )
    return [(row.id, float(row.score)) for row in rows]
