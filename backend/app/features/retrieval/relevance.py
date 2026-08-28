"""Whether the corpus has anything to say about this at all.

The dense half returns the k nearest neighbours however far away they are — a kNN has no
notion of "nothing is close" — so a search for the melting point of tungsten over a corpus
of Spanish employment law comes back with eight ranked passages and every appearance of
having found something. Chat does not have this problem, but only because it asks a model:
its abstention is the model's judgement, not retrieval's, and repeating that would mean a
generation on every search.

The cross-encoder is already computing the judgement. It reads question and passage together
and is trained on exactly "does this answer that", and TEI applies the sigmoid, so its output
is a probability rather than a logit — verified, not assumed: querying it directly with
"tungsteno" against a passage on the forty-hour week returns 0.011, and against a passage on
tungsten returns 0.59.

**Three bands, not one threshold, and the reason is an asymmetry.** A false negative here is
far worse than a false positive: hide a passage that was really there and the reader
concludes the corpus does not contain it, with no way to find out otherwise, because the
screen told them there was nothing to check. A false positive costs them reading two
paragraphs and deciding for themselves — which is what they came to do.

So `NONE` hides, and it is set where the measurement says it hides nothing real. `WEAK`
hides nothing at all; it only says the match is poor, which the client renders as a line
above the results.

## What the cross-encoder is sensitive to, and it is not what you would guess.

Measured on the same question written three ways:

    "¿Cuántos goles marcó Messi en 2012?"   0.0775   weak
    "Cuantos goles marco Messi en 2012?"    0.0678   weak
    "cuantos goles marco Messi en 2012"     0.3096   confident

The accents make no difference; the **question mark** does. A cross-encoder is trained on
question-passage pairs, so without one the input reads as a keyword string — and a page of
tax tables is a plausible match for a bag of numbers. Put it back and it is judged as a
question again, which a table does not answer.

That matters because the runbook makes a point of typing without accents, and people
search in keywords. **The failure is in the cheap direction** — a keyword query about
nothing comes back `confident` instead of `weak`, which shows results that were going to
be shown anyway, without the notice. Nothing is hidden by it.

`WEAK_CEILING` was deliberately not raised to 0.32 to catch this one case: doing so marks
`attention-optimizer`, `boe-bank-rate` and `cross-eu-extraterritorial` weak, which is
three correct answers wearing a notice that says nothing matches. Seven scored negatives
is a small sample and moving a measured constant to chase the newest failure in it is how
a threshold becomes overfitted to its own test set.
"""

from __future__ import annotations

from enum import StrEnum


class Relevance(StrEnum):
    """How much the corpus has to say. Part of the response contract, not diagnostics."""

    CONFIDENT = "confident"
    WEAK = "weak"
    NONE = "none"


#: Below this, nothing is shown.
#:
#: Measured, `eval/separation.json`, 2026-08-28, 30 answerable and 10 unanswerable questions
#: against the 26-document corpus: at 0.02 four of the ten negatives are rejected and
#: **zero of the thirty answerable questions are hidden**. That second number is the one
#: that sets this constant. Raising it to 0.15 would reject nine of ten — but it would put
#: `cross-payroll-and-return` (0.0272) below the floor, and hiding a real answer is the
#: failure this whole module is shaped to avoid.
FLOOR = 0.02

#: Between `FLOOR` and this, results are shown and the poor match is stated.
#:
#: 0.15 rather than 0.20, and the gap is not arbitrary: three answerable questions score
#: between 0.16 and 0.19 — `attention-optimizer`, `boe-bank-rate`,
#: `cross-eu-extraterritorial` — so 0.20 would call three good answers weak. Below 0.16 the
#: band catches five more negatives at no cost to any of them.
#:
#: The distribution is strongly bimodal — most observations above 0.9 or below 0.2 — and the
#: first run of 40 questions had **nothing at all between 0.2 and 0.4**, which was written
#: down here as an empty valley the line could sit in safely.
#:
#: **That claim did not survive the 41st question.** `cuantos goles marco Messi en 2012`
#: scores 0.3096, in the middle of it. The valley was an artefact of the sample size, and a
#: constant defended by "there is nothing near it" is only ever as true as the last thing
#: measured. The line stays where the trade-off puts it, not where the gap looked widest.
WEAK_CEILING = 0.15


def classify(top_rerank: float | None) -> Relevance:
    """What the best passage's cross-encoder score says about the whole result set.

    The best one only. A set where the top passage plainly answers the question is a good
    set even if the eighth is noise — that is what ranking is for — and averaging would let
    seven weak passages outvote the one that is right.

    `None` means no reranker ran: off on this profile, or down. **The answer is `CONFIDENT`,
    deliberately.** Without the signal there is no evidence of absence, and inventing a
    verdict from its silence would hide results on exactly the installations least able to
    afford it. `ADR 0005` puts it the other way round — a profile may change cost, never an
    outcome — and a screen that says "nothing matches" on one machine and returns eight
    passages on another is that rule broken.
    """
    if top_rerank is None:
        return Relevance.CONFIDENT
    if top_rerank < FLOOR:
        return Relevance.NONE
    if top_rerank < WEAK_CEILING:
        return Relevance.WEAK
    return Relevance.CONFIDENT


def best_rerank(scores: list[float | None]) -> float | None:
    """The highest score actually returned, or `None` if none were."""
    present = [score for score in scores if score is not None]
    return max(present) if present else None
