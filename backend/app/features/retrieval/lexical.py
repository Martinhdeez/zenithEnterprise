"""The query side of lexical search, and the bug that nearly cost us ParadeDB.

`chunks.tsv` is a generated column: `to_tsvector('english', text)`. The corpus side is
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

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

CONFIGURATION = "english"


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
