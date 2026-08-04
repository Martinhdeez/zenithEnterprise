# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""The end-to-end run: question in, cited answer out, scored against the anchors.

Three numbers, and the third is the one this product is sold on.

**Quotation rate** — the answer contains the anchor, the exact phrase the fact comes from.
The F9 spec predicted this would be an *upper* bound on correctness, on the reasoning that
containment is lenient. **The first run disproved that**, and the correction is recorded
here rather than quietly fixed:

    anchor "DensePassageRetriever"   answer "Dense Passage Retriever (DPR)"   scored MISS
    anchor "this is Bank Rate"       a correct definition of Bank Rate        scored MISS

Of the first four misses inspected by hand, three were correct answers. Typography is now
handled (`contains` retries on alphanumerics only), but paraphrase is not and cannot be —
an anchor lifted from a section heading will never appear verbatim in good prose.

So this number is **neither an upper nor a lower bound on correctness**. It measures
whether the answer quotes the source, which is a real property worth tracking and is not
the same property as being right. It is named for what it measures.

What it *is* trustworthy for is the direction this project cares about: a fabricated answer
does not contain an anchor, because an anchor is a string lifted from the document.

**Citation validity** — every citation resolves to a passage that was in the shortlist. It
is 100% by construction, since `citations.bind` strips anything else, so this is a
regression test on the 0% gate rather than a discovery.

**Abstention** — the five `unanswerable` questions, scored for the first time. They have
been in the set since M0 and recall could not score them: there is no correct passage, so
recall over them is undefined. Generation makes them scorable, and `unanswerable-tungsten`
is the sharpest — the melting point of tungsten is not in the corpus and *is* in the
model's weights, so answering it is the model being helpful with knowledge it was told not
to use. A system that answers it will answer a question about a customer's contract the
same way.

The provider comes from F8's registry, so this harness contains no vendor name and runs
against Ollama, vLLM or anything else by changing `ZENITH_LLM_PROVIDER`.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from eval.grounding import contains, ingested_profile

REPORT = Path(__file__).resolve().parent / "answer-report.json"


@dataclass
class Answered:
    question_id: str
    type: str
    abstained: bool = False
    # The anchor appears in the answer text. This measures *quotation*, not correctness —
    # see the module docstring for the measured false-negative rate.
    quoted: bool = False
    citations: int = 0
    # Citations naming a passage that was not in the shortlist. Zero by construction; if it
    # is ever non-zero, `citations.bind` has a hole.
    invalid_citations: int = 0
    fabricated_markers: int = 0
    elapsed_ms: float = 0.0
    answer: str = ""


@dataclass
class Summary:
    answered: list[Answered] = field(default_factory=list[Answered])


async def reachable() -> str | None:
    """Whether a provider will actually answer, checked before spending an hour finding out.

    Returns the model name on success and `None` otherwise. A harness that discovers the
    model is down on question 30 of 36 has wasted the run, and one that treats an
    unreachable model as a wrong answer would report a fabrication rate that is really a
    connectivity problem.
    """
    from app.common.llm import GenerationUnavailableError
    from app.features.generation import providers

    try:
        provider = providers.build(providers.from_settings())
        completion = await provider.complete("Reply with the single word: ready.", "ready?")
    except GenerationUnavailableError as exc:
        print(f"no provider reachable: {exc.message}")
        return None
    return completion.model


async def measure() -> list[Answered]:
    from app.core.hardware import PROFILES
    from app.features.generation.service import AnswerService
    from app.features.retrieval.service import SearchService
    from eval.production import LocalQueryEmbedder, LocalReranker
    from eval.questions import load_questions

    profile = await ingested_profile()
    assert profile is not None, "checked by the caller"

    search = SearchService(
        profile,  # type: ignore[arg-type]
        embedder=LocalQueryEmbedder(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        reranker=LocalReranker(),  # type: ignore[arg-type]
    )
    service = AnswerService(profile, search=search)  # type: ignore[arg-type]

    results: list[Answered] = []
    for question in load_questions():
        started = time.perf_counter()
        answer = await service.answer(question.question)
        elapsed = (time.perf_counter() - started) * 1000

        anchors = [source.anchor for source in question.sources]
        shortlist = {hit.chunk_id for hit in answer.consulted}
        result = Answered(
            question_id=question.id,
            type=question.type,
            abstained=answer.abstained,
            quoted=any(contains(answer.answer, anchor) for anchor in anchors),
            citations=len(answer.citations),
            invalid_citations=sum(
                citation.chunk_id not in shortlist for citation in answer.citations
            ),
            elapsed_ms=elapsed,
            answer=answer.answer,
        )
        results.append(result)

        verdict = "ABSTAINED" if result.abstained else ("quoted" if result.quoted else "no anchor")
        print(
            f"{question.id:<26} {question.type:<15} {verdict:<10} "
            f"cites={result.citations} {elapsed:>7.0f}ms",
            flush=True,
        )
    return results


def report(results: list[Answered], model: str) -> dict[str, object]:
    answerable = [item for item in results if item.type != "unanswerable"]
    unanswerable = [item for item in results if item.type == "unanswerable"]
    kinds = sorted({item.type for item in answerable})

    def rate(items: list[Answered], attribute: str) -> float:
        if not items:
            return 0.0
        return round(sum(getattr(item, attribute) for item in items) / len(items), 4)

    return {
        "model": model,
        "questions": len(results),
        # The headline. Upper bound: containment, not comprehension.
        "quotation_rate": rate(answerable, "quoted"),
        "abstention_rate_on_answerable": rate(answerable, "abstained"),
        # The gate. Anything other than zero here is a defect in `citations.bind`.
        "invalid_citations": sum(item.invalid_citations for item in results),
        "abstention": {
            "unanswerable_questions": len(unanswerable),
            "correctly_abstained": sum(item.abstained for item in unanswerable),
            "answered_anyway": [item.question_id for item in unanswerable if not item.abstained],
        },
        "by_type": {
            kind: {
                "quoted": rate([i for i in answerable if i.type == kind], "quoted"),
                "abstained": rate([i for i in answerable if i.type == kind], "abstained"),
            }
            for kind in kinds
        },
        "median_latency_ms": round(
            sorted(item.elapsed_ms for item in results)[len(results) // 2], 1
        ),
        "unquoted": [
            item.question_id for item in answerable if not item.quoted and not item.abstained
        ],
    }


async def run() -> None:
    if await ingested_profile() is None:
        print("No ingested corpus found. Run `uv run python -m eval.production` first.")
        return

    model = await reachable()
    if model is None:
        print(
            "\nSet ZENITH_LLM_ENDPOINT_URL and ZENITH_LLM_MODEL, or start the local "
            "baseline:\n  ollama serve && ollama pull llama3.1:8b-instruct-q4_K_M"
        )
        raise SystemExit(1)

    print(f"answering through {model}\n", flush=True)
    results = await measure()
    summary = report(results, model)
    print("\n" + json.dumps(summary, indent=2))
    REPORT.write_text(
        json.dumps({"summary": summary, "results": [vars(r) for r in results]}, indent=2)
    )


if __name__ == "__main__":
    asyncio.run(run())
