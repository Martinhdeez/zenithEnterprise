"""Where the 0% gate is actually enforced.

mvp.md 5.6 has one metric whose target is zero: **fabrication with a false citation**. A
gate of zero cannot be met by asking nicely. Prompts are advice, and an 8B model takes
advice about as well as an intern on their second day — so what comes back from the model
is treated as a claim to be checked, not a result to be rendered.

Three rules, in order of how much they cost:

1. A marker naming a passage that was not sent is **removed from the text**. The model
   cannot cite what it was not shown, because a citation is a promise the user can click.
2. An answer with no valid citation at all is **discarded** and replaced by the abstention.
3. Only markers that survive become rows in `query_citations`.

Rule 2 is the strict one, and it is deliberate. An uncited answer may well be correct —
and there is no way to tell it apart from an invented one without reading the corpus, which
is the work the user came here to avoid. Shipping it would make the gate measure nothing:
the fabrications would simply stop wearing markers.
"""

import re
from dataclasses import dataclass
from uuid import UUID

import structlog

from app.features.generation.prompt import ABSTENTION
from app.features.retrieval.search import Hit

log = structlog.get_logger()

# `[3]`, and nothing cleverer. Models also write `[1, 2]` and `[1][2]`; the first is handled
# by allowing a comma-separated list inside one pair of brackets, the second falls out for
# free. Anything more exotic is not matched, so it stays in the text as literal characters
# and cites nothing — the safe direction to fail in.
MARKER = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


@dataclass(frozen=True, slots=True)
class Citation:
    """One passage the answer actually leaned on.

    `marker` is the number as it appears in the text, so a viewer can highlight the right
    bracket; everything else is what a click has to resolve to — the document, the page, and
    the boxes to draw on it.
    """

    marker: int
    chunk_id: UUID
    document_id: UUID
    filename: str
    page_num: int
    text: str
    bboxes: list[dict[str, float]]


@dataclass(frozen=True, slots=True)
class Bound:
    answer: str
    citations: list[Citation]
    abstained: bool
    # Markers the model invented. Kept as a count rather than dropped silently, because
    # this is the number the RNF-06 certification suite scores a model on, and a model that
    # invents citations is one a customer needs telling about.
    fabricated: int


def bind(answer: str, hits: list[Hit]) -> Bound:
    valid = range(1, len(hits) + 1)
    cited: dict[int, Citation] = {}
    fabricated = 0

    def keep(match: re.Match[str]) -> str:
        """Rewrite one marker, dropping the numbers that name nothing.

        Done as a substitution rather than by deleting characters at recorded offsets,
        because a `[1, 9]` has to become `[1]` rather than disappear — the valid half of a
        half-invented citation is still a real reference to a real passage.
        """
        nonlocal fabricated
        numbers = [int(part) for part in match.group(1).split(",")]
        survivors = [number for number in numbers if number in valid]
        fabricated += len(numbers) - len(survivors)
        for number in survivors:
            cited.setdefault(number, _citation(number, hits[number - 1]))
        return "".join(f"[{number}]" for number in survivors)

    cleaned = MARKER.sub(keep, answer)
    if fabricated:
        log.warning("citation_fabricated", count=fabricated, passages=len(hits))

    if _is_abstention(cleaned):
        # The model was asked for this sentence exactly, and it said it. No citations: it
        # is a statement about the corpus, not about any passage in it.
        return Bound(answer=ABSTENTION, citations=[], abstained=True, fabricated=fabricated)

    if not cited:
        log.warning("answer_discarded_uncited", passages=len(hits), length=len(answer))
        return Bound(answer=ABSTENTION, citations=[], abstained=True, fabricated=fabricated)

    return Bound(
        answer=_tidy(cleaned),
        citations=[cited[number] for number in sorted(cited)],
        abstained=False,
        fabricated=fabricated,
    )


def _citation(marker: int, hit: Hit) -> Citation:
    return Citation(
        marker=marker,
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        filename=hit.filename,
        page_num=hit.page_num,
        text=hit.text,
        bboxes=hit.bboxes,
    )


def _is_abstention(answer: str) -> bool:
    """Recognised by the sentence the model was handed, not by sentiment.

    Deliberately loose about surrounding whitespace and a trailing full stop, and
    deliberately strict about everything else: a model that writes its own way of saying "I
    don't know" produces an uncited answer, which rule 2 turns into an abstention anyway.
    """
    return answer.strip().rstrip(".").lower() == ABSTENTION.rstrip(".").lower()


def _tidy(answer: str) -> str:
    """Repair the spacing a removed marker leaves behind.

    Stripping `[9]` from "as stated [9] in the contract" leaves a double space, and from
    "...applies [9]." leaves a space before the full stop. Cosmetic, and the reason it is
    here rather than ignored: a visibly mangled sentence reads as a bug in the answer, and
    invites the user to distrust the citation that survived next to it.
    """
    return re.sub(r" +([.,;:])", r"\1", re.sub(r"[ \t]{2,}", " ", answer)).strip()
