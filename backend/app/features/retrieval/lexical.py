"""The query side of lexical search, and the bug that nearly cost us ParadeDB.

`chunks.tsv` is a generated column: `to_tsvector('zenith_text', text)`. The corpus side is
therefore tokenised by Postgres, with Postgres's rules.

M0's first implementation tokenised the *query* with `re.findall(r"[A-Za-z0-9']+", ...)`.
That splits `119/33` into `119`, and `1545-0074` into `1545` and `0074`, while
`to_tsvector` had stored both whole. The corpus side preserved identifiers and the query
side destroyed them, so lexical search found **one identifier in six** and looked worthless
next to dense retrieval. The conclusion nearly drawn from that number was that ParadeDB was
not worth its dependency.

With the query tokenised by the same analyser that built the index: **four in six, and two
of those are lexical-only** — dense retrieval cannot find them at all. The hybrid design is
justified by evidence rather than by intuition, and it took fixing our own bug to see it.

The rule this leaves behind: **never tokenise a query with anything but the analyser that
built the index.** A regex that looks equivalent is not, and the failure is silent — it
does not error, it just returns worse results.
"""

from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

#: The implementations `ZENITH_LEXICAL_ENGINE` may name.
#:
#: `tsvector` is `ts_rank_cd` over the GIN index — every recall figure in `eval/` was
#: measured against it. `bm25` is ParadeDB resolving the top N inside its own index, which
#: is the only one of the two that does not cost time linear in rows matched.
ENGINES: Final[frozenset[str]] = frozenset({"tsvector", "bm25"})


def engine() -> str:
    """The configured lexical engine, or a startup failure.

    Same reasoning as `hardware.active()`: an operator who types `ZENITH_LEXICAL_ENGINE=BM25`
    gets an error naming the valid values, not a silent fallback to the other implementation
    and a recall number nobody can explain.
    """
    if settings.lexical_engine not in ENGINES:
        raise ValueError(
            f"ZENITH_LEXICAL_ENGINE is {settings.lexical_engine!r}, which is not an engine. "
            f"Valid values: {', '.join(sorted(ENGINES))}."
        )
    return settings.lexical_engine


#: The configuration both sides use. `zenith_text` is `english` with `unaccent` in front of
#: the stemmer — migration 0018 — so `maximo` finds `máximo` and every English rule is
#: unchanged. The name is read here and by the generated column's definition, which is what
#: keeps the two ends tokenised by the same analyser.
CONFIGURATION = "zenith_text"


async def to_tsquery(session: AsyncSession, question: str) -> str:
    """The question as an OR'd `tsquery`, tokenised by Postgres itself.

    OR rather than AND because a question is not a filter: requiring every lexeme returns
    nothing for anything conversational, and ranking already puts the passages containing
    more of them first. Fusion with the dense half handles the rest.
    """
    lexemes = await session.scalar(
        text("SELECT array_to_string(tsvector_to_array(to_tsvector(:config, :question)), ' | ')"),
        {"config": CONFIGURATION, "question": question},
    )
    return str(lexemes or "")
