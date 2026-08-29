"""Can retrieval tell a question it can answer from one it cannot?

Chat can. It sends the passages to a model, the model says none of them answer, and
`citations.py` replaces the answer with the abstention. Search cannot, and the reason is
structural rather than a ranking fault: **the dense half returns the k nearest neighbours
however far away they are.** A kNN has no notion of "nothing is close". Ask for the melting
point of tungsten over a corpus of Spanish employment law and eight passages come back,
ranked, with scores.

Doing what Chat does would mean a generation on every search, which is a cost and a latency
this product deliberately refused. So the question this file exists to answer is whether a
**signal already computed** can make the same judgement:

    Do the scores of a question the corpus answers, and the scores of one it does not,
    separate far enough apart to put a line between them?

If they do, a threshold exists and F26 is buildable. If they overlap, no threshold exists,
no amount of tuning will invent one, and the design has to change. **Measuring that is the
whole point — this file changes nothing in the product and is not meant to.**

Four signals per question, because they fail differently:

- `rerank` — the cross-encoder, reading question and passage *together*. The one signal
  actually trained to answer "does this passage answer this question?", and the only real
  candidate. Absent when the reranker is off or down, which is why the others are recorded.
- `dense` — cosine similarity. Weak alone: not comparable across questions, since a
  three-word question and a twenty-word one have different score distributions.
- `lexical_hits` — how many of the returned passages the lexical half matched at all.
  Presence is informative even though the magnitude is not: `ts_rank_cd` has no IDF.
- `spread` — rank 1's fused score over rank 8's. Relative, so it needs no calibration
  across questions; the hypothesis is that nonsense produces a flat distribution because
  nothing stands out. A corpus of near-identical legal passages may flatten it for good
  questions too, which is exactly what this measures rather than assumes.

The five `unanswerable` questions in `questions.toml` are the negatives. `live.py` drops
them on its first line — `if q.sources` — and it is right to: recall over a question with no
answer is undefined. That is also why this gap survived. The number that would have shown it
was never computed.
"""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

# The same map `live.py` uses. Imported rather than copied: two lists of which
# question points at which file would drift, and the drift would look like a
# retrieval failure.
from eval.live import FILENAMES
from eval.questions import load_questions

REPORT = Path(__file__).parent / "separation.json"


@dataclass(frozen=True, slots=True)
class Measured:
    id: str
    kind: str
    """`answerable` or `unanswerable` — the label the threshold has to reproduce."""
    question: str
    hits: int
    rerank: float | None
    dense: float | None
    lexical_hits: int
    spread: float | None
    took_ms: int
    degraded: bool


Hit = dict[str, object]


def _number(hit: Hit, key: str) -> float | None:
    value = hit.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _top(hits: list[Hit], key: str) -> float | None:
    values = [v for v in (_number(hit, key) for hit in hits) if v is not None]
    return max(values) if values else None


def measure(url: str, token: str, limit: int = 8) -> list[Measured]:
    questions = load_questions()

    with httpx.Client(
        base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=120.0
    ) as client:
        ready = {
            document["filename"]
            for document in client.get("/documents", params={"limit": 200}).json()["items"]
            if document["status"] == "ready"
        }

        out: list[Measured] = []
        for question in questions:
            # An answerable question whose document is not loaded would look like a negative
            # and poison the very distribution being measured. A negative has no document to
            # check, so it is always in.
            if question.sources:
                needed = {FILENAMES.get(s.document, s.document) for s in question.sources}
                if not needed <= ready:
                    continue

            started = time.perf_counter()
            response = client.get("/search", params={"q": question.question, "limit": limit})
            response.raise_for_status()
            payload = response.json()
            hits = payload.get("hits", [])

            scores = [v for v in (_number(hit, "score") for hit in hits) if v is not None]
            out.append(
                Measured(
                    id=question.id,
                    kind="unanswerable" if not question.sources else "answerable",
                    question=question.question,
                    hits=len(hits),
                    rerank=_top(hits, "rerank_score"),
                    dense=_top(hits, "dense_score"),
                    lexical_hits=sum(1 for hit in hits if hit.get("lexical_rank") is not None),
                    # Guarded: one hit has no tail to stand above, and a corpus can return
                    # one. Dividing by it would report a spread that means nothing.
                    spread=(scores[0] / scores[-1]) if len(scores) > 1 and scores[-1] else None,
                    took_ms=int((time.perf_counter() - started) * 1000),
                    degraded=bool(payload.get("degraded")),
                )
            )
        return out


@dataclass(frozen=True, slots=True)
class Band:
    low: float
    median: float
    high: float


@dataclass(frozen=True, slots=True)
class Verdict:
    """One signal, and whether a line can be drawn through it."""

    answerable: Band | None
    unanswerable: Band | None
    #: True exactly when the worst question the corpus *can* answer still scores above the
    #: best it cannot. Anything less means every threshold either hides a real answer or
    #: admits a nonsense one — and hiding a real answer is the failure this product cannot
    #: afford, since the user is then told there is nothing to check.
    separates: bool | None
    gap: float | None


def _band(values: list[float]) -> Band | None:
    if not values:
        return None
    ordered = sorted(values)
    return Band(
        low=round(ordered[0], 4),
        median=round(statistics.median(ordered), 4),
        high=round(ordered[-1], 4),
    )


def _verdict(good: list[float], bad: list[float]) -> Verdict:
    low, high = _band(good), _band(bad)
    separates = min(good) > max(bad) if good and bad else None
    return Verdict(
        answerable=low,
        unanswerable=high,
        separates=separates,
        gap=round(min(good) - max(bad), 4) if good and bad else None,
    )


SIGNALS: tuple[tuple[str, Callable[[Measured], float | None]], ...] = (
    ("rerank", lambda m: m.rerank),
    ("dense", lambda m: m.dense),
    ("spread", lambda m: m.spread),
)


def report(measured: list[Measured]) -> int:
    answerable = [m for m in measured if m.kind == "answerable"]
    negative = [m for m in measured if m.kind == "unanswerable"]

    if not negative:
        print("No unanswerable questions loaded — there is nothing to separate against.")
        return 2

    verdicts = {
        name: _verdict(
            [v for v in (getter(m) for m in answerable) if v is not None],
            [v for v in (getter(m) for m in negative) if v is not None],
        )
        for name, getter in SIGNALS
    }
    degraded = sum(m.degraded for m in measured)

    REPORT.write_text(
        json.dumps(
            {
                "answerable_questions": len(answerable),
                "unanswerable_questions": len(negative),
                "degraded_runs": degraded,
                "signals": {name: asdict(v) for name, v in verdicts.items()},
                "measurements": [asdict(m) for m in measured],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )

    print(f"answerable {len(answerable)}   unanswerable {len(negative)}")
    if degraded:
        # A degraded run has no rerank score at all, so the signal this measurement is about
        # is missing from it. Reported loudly rather than averaged in.
        print(f"WARNING: {degraded} searches came back degraded")
    for name, v in verdicts.items():
        if v.answerable is None or v.unanswerable is None:
            print(f"  {name:<8} not available")
            continue
        print(
            f"  {name:<8} answerable {v.answerable.low}–{v.answerable.high}"
            f"   unanswerable {v.unanswerable.low}–{v.unanswerable.high}"
            f"   {'SEPARATES' if v.separates else 'overlaps'}"
        )
    print(f"\nWritten to {REPORT}")
    return 0


def run(url: str, token: str) -> int:
    return report(measure(url, token))
