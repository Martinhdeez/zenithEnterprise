"""What the user is told when part of the search is not working.

The product degrades rather than fails — ADR 0006 — and says so, which is the right call and
the reason `degraded` is in every response. What it *said*, though, was written for the person
who built it:

    semantic search unavailable (ConnectError); lexical only
    reranking unavailable (circuit open); fused order

`ConnectError` is a Python class name and "fused order" is the name of an algorithm. Both were
carried straight to the screen, in front of whoever is being shown the product. The reader is
not debugging; they are deciding whether to trust the answer on the page, and the only thing
that helps them is knowing **what is different about these results**.

So the sentence is written for them, and the exception name goes where the person who can act
on it will look — the structured log, and `zenith diagnose`, which already reports both model
services by name and says which model each is serving. Nothing is lost and nothing is hidden;
the two audiences are simply given different sentences, which is what they always needed.

Named constants rather than strings at the raise site, because these are the words a customer
reads. They deserve to be somewhere a person can review them all at once.
"""

from typing import Final

#: The dense half could not be reached, so the answer came from the lexical half alone.
#:
#: Says what is missing — meaning-based matching — rather than which component is down. A
#: reader who searched for a paraphrase and got nothing needs to understand that this is why.
SEMANTIC_UNAVAILABLE: Final = (
    "Semantic search is temporarily unavailable — these results come from keyword "
    "matching only, so wording matters more than usual."
)

#: The cross-encoder did not answer, so the results are in the fused order.
#:
#: Deliberately reassuring about the part that is *not* affected. Reranking changes the order
#: of the results, not which documents were found, and a warning that does not say so reads
#: as "these results are wrong" — which would be the more damaging misunderstanding.
RERANKING_UNAVAILABLE: Final = (
    "Advanced result ordering is temporarily unavailable — the same documents were "
    "found, but their order is less refined than usual."
)
