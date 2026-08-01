# pyright: reportCallIssue=false, reportArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# psycopg 3.3 types `execute` against `Template`, and sentence-transformers ships partial
# stubs, so strict mode reports every call here as unknown. Suppressed once per file rather
# than on twenty individual lines. Nothing under `app/` relaxes strictness — this is
# laboratory code that never ships, and the M0 plan says so explicitly.

"""Recall, MRR, and the breakdown that stops a false alarm.

The headline is Recall@8 over factual and cross-document questions **only**. Table
questions are expected near zero because the tracer bullet flattens tables by design, and
unanswerable questions have no correct passage at all — recall is undefined for them, and
averaging them in scores correct abstention as failure.

Both are measured and reported. Neither belongs in the number that decides whether the
architecture works.
"""

import json
import statistics
import time
from dataclasses import asdict, dataclass, field

from eval.corpus import load_manifest
from eval.pipeline import (
    chunk_locations,
    connect,
    embed,
    fuse,
    search_dense,
    search_lexical,
)
from eval.questions import Question, load_questions

CANDIDATES = 50
TOP_K = (8, 50)


@dataclass
class Outcome:
    question_id: str
    type: str
    hit_at: dict[int, bool] = field(default_factory=dict)
    document_hit_at: dict[int, bool] = field(default_factory=dict)
    first_rank: int | None = None
    lexical_found: bool = False
    dense_found: bool = False
    elapsed_ms: float = 0.0


def _expected(question: Question) -> set[tuple[str, int]]:
    return {(source.document, page) for source in question.sources for page in source.pages}


def _rank_of_first_hit(
    results: list[tuple[str, int]], expected: set[tuple[str, int]]
) -> int | None:
    for rank, location in enumerate(results, start=1):
        if location in expected:
            return rank
    return None


def evaluate(question: Question, vector: list[float], connection: object) -> Outcome:
    from psycopg import Connection

    assert isinstance(connection, Connection)
    outcome = Outcome(question_id=question.id, type=question.type)
    started = time.perf_counter()

    lexical = search_lexical(connection, question.question, CANDIDATES)
    dense = search_dense(connection, vector, CANDIDATES)
    fused = fuse([lexical, dense])

    outcome.elapsed_ms = (time.perf_counter() - started) * 1000

    if question.type == "unanswerable":
        # Nothing to be right about. Recorded so the count is visible, never averaged in.
        return outcome

    expected = _expected(question)
    expected_documents = {document for document, _ in expected}

    for k in TOP_K:
        locations = chunk_locations(connection, fused[:k])
        outcome.hit_at[k] = any(location in expected for location in locations)
        outcome.document_hit_at[k] = any(
            document in expected_documents for document, _ in locations
        )

    outcome.first_rank = _rank_of_first_hit(chunk_locations(connection, fused), expected)
    # Which half found it, measured separately: if one contributes nothing anywhere, hybrid
    # search is not earning its complexity.
    outcome.lexical_found = any(
        location in expected for location in chunk_locations(connection, lexical)
    )
    outcome.dense_found = any(
        location in expected for location in chunk_locations(connection, dense)
    )
    return outcome


def corpus_reach(
    connection: object,
    questions: list[Question],
    vectors: list[list[float]],
    share: float,
) -> float:
    """Recall@50 when the caller reaches only part of the corpus.

    The access-label case (RF-04.2), and the one that motivated the whole partitioning
    discussion: filtering inside a vector query makes HNSW walk further to gather the same
    number of candidates. `mvp.md` §8 gates the drop at 5%.

    The documents a question needs are always included, so this measures the *cost of
    filtering*, not the cost of hiding the answer.
    """
    from psycopg import Connection

    assert isinstance(connection, Connection)
    all_documents = [document.id for document in load_manifest() if document.path.exists()]
    keep = max(1, round(len(all_documents) * share))

    hits = 0
    counted = 0
    for question, vector in zip(questions, vectors, strict=True):
        if question.type == "unanswerable" or not question.counts_towards_headline:
            continue
        needed = sorted({source.document for source in question.sources})
        others = [document for document in all_documents if document not in needed]
        visible = needed + others[: max(0, keep - len(needed))]

        results = chunk_locations(connection, search_dense(connection, vector, 50, visible))
        hits += any(location in _expected(question) for location in results)
        counted += 1
    return hits / counted if counted else 0.0


def run() -> dict[str, object]:
    questions = load_questions()
    vectors = embed([question.question for question in questions], batch=8)

    connection = connect()
    outcomes = [
        evaluate(question, vector, connection)
        for question, vector in zip(questions, vectors, strict=True)
    ]

    headline = [
        outcome
        for outcome, question in zip(outcomes, questions, strict=True)
        if question.counts_towards_headline
    ]
    by_type: dict[str, list[Outcome]] = {}
    for outcome in outcomes:
        by_type.setdefault(outcome.type, []).append(outcome)

    def recall(group: list[Outcome], k: int) -> float:
        scored = [o for o in group if o.type != "unanswerable"]
        return sum(o.hit_at.get(k, False) for o in scored) / len(scored) if scored else 0.0

    def document_recall(group: list[Outcome], k: int) -> float:
        scored = [o for o in group if o.type != "unanswerable"]
        return sum(o.document_hit_at.get(k, False) for o in scored) / len(scored) if scored else 0.0

    def mrr(group: list[Outcome]) -> float:
        scored = [o for o in group if o.type != "unanswerable"]
        return (
            sum(1 / o.first_rank if o.first_rank else 0.0 for o in scored) / len(scored)
            if scored
            else 0.0
        )

    report: dict[str, object] = {
        "headline": {
            "recall@8": recall(headline, 8),
            "recall@50": recall(headline, 50),
            "document_recall@8": document_recall(headline, 8),
            "mrr": mrr(headline),
            "questions": len(headline),
        },
        "by_type": {
            name: {
                "recall@8": recall(group, 8),
                "recall@50": recall(group, 50),
                "questions": len(group),
            }
            for name, group in sorted(by_type.items())
        },
        "halves": {
            "lexical_only": sum(o.lexical_found and not o.dense_found for o in headline),
            "dense_only": sum(o.dense_found and not o.lexical_found for o in headline),
            "both": sum(o.lexical_found and o.dense_found for o in headline),
            "neither": sum(not o.lexical_found and not o.dense_found for o in headline),
        },
        # The diagnostic the first pass could not run, because it had no identifier
        # questions: does the lexical half earn its place on exact strings?
        "halves_on_identifiers": {
            "lexical_found": sum(o.lexical_found for o in by_type.get("identifier", [])),
            "dense_found": sum(o.dense_found for o in by_type.get("identifier", [])),
            "lexical_only": sum(
                o.lexical_found and not o.dense_found for o in by_type.get("identifier", [])
            ),
            "questions": len(by_type.get("identifier", [])),
        },
        "latency_ms": {
            "median": statistics.median(o.elapsed_ms for o in outcomes),
            "note": "Apple M4 Pro, 24 GB — NOT customer hardware. Not a gate. See M0 plan §3.4.",
        },
        "corpus_reach": {
            "100%": corpus_reach(connection, questions, vectors, 1.0),
            "20%": corpus_reach(connection, questions, vectors, 0.2),
            "5%": corpus_reach(connection, questions, vectors, 0.05),
        },
        "outcomes": [asdict(outcome) for outcome in outcomes],
    }
    connection.close()
    return report


if __name__ == "__main__":
    result = run()
    print(json.dumps({k: v for k, v in result.items() if k != "outcomes"}, indent=2, default=str))
