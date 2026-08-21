# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""Recall@8 of the **running installation**, over HTTP, as a user would experience it.

`production.py` measures the retrieval path in-process with PyTorch embeddings: it answers
"does the ranking design work". This answers a different question — *"is the thing I am
about to demonstrate actually good"* — and it can only be answered through the deployment,
because that is where the reranker, the hardware profile, TEI's batch limits and the
circuit breaker all exist. Every one of those has silently degraded search at least once in
this project's history, and none of them is reachable from a unit test.

The credit rule is `questions.toml`'s: a hand-verified `anchor` phrase appearing in a
returned passage. The anchors are re-derived from the documents by `tests/test_questions.py`,
so this is not grading against something somebody typed from memory.

**Questions whose source document is not in the corpus are excluded, not counted as
misses.** Scoring them would measure which files happen to have been uploaded rather than
how well retrieval ranks, and a number that moves when somebody deletes a PDF is not a
quality metric. The count of exclusions is always printed, because a great score over three
questions is not a great score.

    python -m eval live --token <jwt>            # against localhost:8000
    python -m eval live --token <jwt> --url http://host:8000
"""

import json
import statistics
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import httpx

from eval.questions import load_questions

#: eval document id -> the filename it carries once uploaded. The two differ because the
#: manifest names documents by what they *are* and the corpus stores what the file was
#: called; keeping the map here means a rename never silently drops a question from the run.
FILENAMES = {
    "gdpr": "gdpr.pdf",
    "irs-1040-instructions": "nuevo-irs.pdf",
    "irs-form-1040": "irs-form-1040.pdf",
    "eu-ai-act": "eu-ai-act.pdf",
    "eu-digital-services-act": "eu-digital-services-act.pdf",
    "irs-pub-15": "irs-pub-15.pdf",
    "bert-paper": "nuevo-bert.pdf",
    "attention-is-all-you-need": "attention-is-all-you-need.pdf",
    "rag-paper": "sin-etiqueta.pdf",
    "boe-monetary-policy": "boe-monetary-policy.pdf",
    "infrastructure-act": "infrastructure-act.pdf",
    "nasa-technical-report": "nasa-technical-report.pdf",
    "nasa-scanned-report": "nasa-scanned-report.pdf",
}

REPORT = Path(__file__).parent / "live-recall.json"


def flatten(text: str) -> str:
    """Lowercased, accent-stripped, whitespace-collapsed.

    Anchors are copied from the PDF and passages come back through extraction, so the two
    disagree on line breaks and — for the Spanish documents — sometimes on accents. Matching
    the raw strings would report misses that a reader looking at both would call hits.
    """
    lowered = unicodedata.normalize("NFKD", text.lower())
    return " ".join("".join(c for c in lowered if not unicodedata.combining(c)).split())


@dataclass(frozen=True, slots=True)
class Outcome:
    question_id: str
    kind: str
    headline: bool
    rank: int | None
    took_ms: int
    degraded: bool


def run(url: str, token: str, limit: int = 8) -> int:
    questions = [q for q in load_questions() if q.sources]

    with httpx.Client(
        base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=120.0
    ) as client:
        ready = {
            document["filename"]
            for document in client.get("/documents", params={"limit": 200}).json()["items"]
            if document["status"] == "ready"
        }

        outcomes: list[Outcome] = []
        skipped: list[str] = []
        for question in questions:
            needed = {FILENAMES.get(s.document, s.document) for s in question.sources}
            if not needed <= ready:
                skipped.append(question.id)
                continue

            started = time.perf_counter()
            response = client.get("/search", params={"q": question.question, "limit": limit})
            response.raise_for_status()
            payload = response.json()
            wall = int((time.perf_counter() - started) * 1000)

            anchors = [flatten(s.anchor) for s in question.sources]
            rank = next(
                (
                    position
                    for position, hit in enumerate(payload.get("hits", []), start=1)
                    if any(anchor in flatten(hit.get("text", "")) for anchor in anchors)
                ),
                None,
            )
            outcomes.append(
                Outcome(
                    question.id,
                    question.type,
                    question.counts_towards_headline,
                    rank,
                    payload.get("took_ms", wall),
                    bool(payload.get("degraded")),
                )
            )

    return report(outcomes, skipped, len(ready))


def recall_of(outcomes: list[Outcome]) -> float | None:
    return sum(o.rank is not None for o in outcomes) / len(outcomes) if outcomes else None


def report(outcomes: list[Outcome], skipped: list[str], corpus: int) -> int:
    if not outcomes:
        print("Nothing to score: no question's documents are in this corpus.")
        return 1

    # `HEADLINE_TYPES` decides the number, exactly as it does for `production.py`. Identifier
    # and table questions are diagnostics on one half of retrieval each — folding them in
    # would make the headline move whenever the lexical implementation changed, which is the
    # opposite of what a baseline is for. They are reported beside it instead.
    headline = [o for o in outcomes if o.headline]
    found = [o for o in outcomes if o.rank is not None]
    latencies = sorted(o.took_ms for o in outcomes)
    by_kind = {
        kind: value
        for kind in sorted({o.kind for o in outcomes})
        if (value := recall_of([o for o in outcomes if o.kind == kind])) is not None
    }

    headline_recall = recall_of(headline) or 0.0
    overall_recall = recall_of(outcomes) or 0.0
    first_place = sum(o.rank == 1 for o in outcomes) / len(outcomes)
    mean_rank = statistics.mean(o.rank for o in found if o.rank is not None) if found else None
    median_ms = latencies[len(latencies) // 2]
    # p95 rather than the mean: the mean hides the one query that takes ten seconds, and
    # that is the query somebody will run in front of an audience.
    p95_ms = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
    degraded = sum(o.degraded for o in outcomes)
    missed = [o.question_id for o in outcomes if o.rank is None]

    print(f"  corpus:       {corpus} documents ready")
    print(f"  scored:       {len(outcomes)} questions ({len(skipped)} skipped, no document)")
    print(
        f"  HEADLINE      Recall@8 {headline_recall:.1%} "
        f"over {len(headline)} factual/cross-document"
    )
    print(f"  all questions Recall@8 {overall_recall:.1%}, Recall@1 {first_place:.1%}")
    print(f"  mean rank:    {mean_rank:.2f}" if mean_rank else "  mean rank:    n/a")
    for kind, value in by_kind.items():
        print(f"     {kind:<16} {value:.1%}")
    print(f"  latency:      {median_ms} ms median, {p95_ms} ms p95")
    if degraded:
        # Loudly, because a degraded run measures the fallback rather than the product, and
        # every number above it is about something the customer is not buying.
        print(f"  DEGRADED:     {degraded} of {len(outcomes)} queries")
    if missed:
        print(f"  missed:       {', '.join(missed)}")

    REPORT.write_text(
        json.dumps(
            {
                "corpus_documents": corpus,
                "scored": len(outcomes),
                "skipped": len(skipped),
                "headline_recall_at_8": round(headline_recall, 4),
                "headline_scored": len(headline),
                "recall_at_8_all": round(overall_recall, 4),
                "recall_at_1_all": round(first_place, 4),
                "mean_rank": round(mean_rank, 2) if mean_rank else None,
                "by_type": {kind: round(value, 4) for kind, value in by_kind.items()},
                "median_ms": median_ms,
                "p95_ms": p95_ms,
                "degraded": degraded,
                "missed": missed,
            },
            indent=2,
        )
        + "\n"
    )
    where = REPORT.relative_to(Path.cwd()) if REPORT.is_relative_to(Path.cwd()) else REPORT
    print(f"\n  written to {where}")
    return 0
