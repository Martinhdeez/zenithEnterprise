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

The credit rule is `production.py`'s, deliberately: a returned passage counts when its
`(document, page)` is one the question records. Those pages are re-derived from the
extracted text by `tests/test_questions.py`, so this is not grading against something
somebody typed from memory — and sharing the rule is what makes the two numbers comparable.

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


@dataclass(frozen=True, slots=True)
class Outcome:
    question_id: str
    kind: str
    headline: bool
    rank: int | None
    #: The right document came back, whatever page. Separates "retrieval never found the
    #: file" from "it found the file and ranked the wrong passage" — two different problems
    #: with two different fixes, and a single recall number cannot tell them apart.
    document_hit: bool
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

            # `(document, page)`, the same credit rule `production.py` uses, and it has to
            # be the same or the two numbers are not comparable.
            #
            # Matching the anchor *text* inside the returned passage looks stricter and is
            # simply wrong: a chunk is ~1,200 characters and a page holds several, so the
            # chunk that answers the question frequently sits on the right page beside the
            # sentence containing the anchor rather than containing it. Scored that way this
            # corpus reported 50% where the page rule reports far more — a measurement
            # artefact that would have been read as a retrieval collapse.
            expected = {
                (FILENAMES.get(s.document, s.document), page)
                for s in question.sources
                for page in s.pages
            }
            hits = payload.get("hits", [])
            rank = next(
                (
                    position
                    for position, hit in enumerate(hits, start=1)
                    if (hit.get("filename"), hit.get("page_num")) in expected
                ),
                None,
            )
            wanted_files = {filename for filename, _ in expected}
            outcomes.append(
                Outcome(
                    question.id,
                    question.type,
                    question.counts_towards_headline,
                    rank,
                    any(hit.get("filename") in wanted_files for hit in hits),
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
    document_recall = sum(o.document_hit for o in headline) / len(headline) if headline else 0.0
    degraded = sum(o.degraded for o in outcomes)
    missed = [o.question_id for o in outcomes if o.rank is None]

    print(f"  corpus:       {corpus} documents ready")
    print(f"  scored:       {len(outcomes)} questions ({len(skipped)} skipped, no document)")
    print(
        f"  HEADLINE      Recall@8 {headline_recall:.1%} "
        f"over {len(headline)} factual/cross-document"
    )
    print(f"  all questions Recall@8 {overall_recall:.1%}, Recall@1 {first_place:.1%}")
    # The gap between this and the headline is the diagnosis: a high document recall with a
    # low passage recall means retrieval is finding the file and ranking the wrong page,
    # which is a chunking or reranking problem rather than a search one.
    print(f"  right document in the page, whatever the page: {document_recall:.1%}")
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
                "document_recall_at_8": round(document_recall, 4),
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
