# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""The chain that bounds answer quality, measured without a language model.

M0 wrote it down and F7 confirmed it: recall finds the page whether or not the table on it
survived extraction. Table questions scored 80% before reranking and 80% after — unmoved by
the best ordering the system can produce, because ordering was never the problem.

So this measures the two stages *underneath* the answer, in order, where each bounds the
next:

    extraction   does the anchor survive parsing at all?      → bounds everything
    context      does it reach the model, in the top 8?       → bounds what it can say

The second is the ceiling on answer quality. No model can state a fact that was not in its
context, so if that number is 60%, a better model is money spent on the wrong stage.

Neither needs an LLM, which is what makes them reproducible on any machine and compliant
with RNF-07 by construction: there is nothing here to send anywhere.
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from eval.questions import Question, load_questions

REPORT = Path(__file__).resolve().parent / "grounding-report.json"

# What the model is given to read. Must match `AnswerService.PASSAGES`, and is asserted
# against it rather than duplicated as a number — a context ceiling measured over a
# different depth than the product uses would be a lie in the most quotable direction.
from app.features.generation.service import PASSAGES  # noqa: E402


@dataclass
class Grounded:
    question_id: str
    type: str
    # The anchor exists somewhere in the extracted text of the pages the question names.
    extracted: bool = False
    # ...and reaches the model, inside the passages actually retrieved.
    in_context: bool = False
    # Where it landed, so a near-miss is distinguishable from an absence.
    context_rank: int | None = None
    anchors: tuple[str, ...] = ()


@dataclass
class Extraction:
    """Per-document extraction, cached across questions.

    Several questions share a document, and parsing `irs-pub-15` is measured in minutes
    rather than seconds. Without this the run costs its own runtime several times over for
    nothing.
    """

    pages: dict[int, str] = field(default_factory=dict[int, str])


def normalise(text: str) -> str:
    """Whitespace-insensitive, case-insensitive comparison.

    A PDF extractor decides for itself where line breaks and double spaces go, and an
    anchor recorded as `CPI inflation is projected` must still match text broken across a
    line. Collapsing whitespace is the difference between measuring extraction and
    measuring the extractor's typography.
    """
    return re.sub(r"\s+", " ", text).strip().lower()


def squashed(text: str) -> str:
    """Alphanumerics only. The second chance for an anchor that differs by typography.

    Measured, not guessed: the RAG paper writes `DensePassageRetriever` in a code font and
    the model answers `Dense Passage Retriever`. Those are the same name, and the first
    version of this check scored the correct answer as a miss.
    """
    return re.sub(r"[^a-z0-9]", "", text.lower())


def contains(haystack: str, anchor: str) -> bool:
    """Whitespace-normalised containment, then a typography-insensitive second chance.

    The second chance is **withheld from anchors containing digits**, and that restriction
    is the whole reason it is safe. Squashing `1.45%` gives `145`, which appears inside
    `1450` and inside a page number — so a numeric anchor would start matching things that
    are not it. Numbers are exact facts where punctuation carries meaning; names are prose
    where the typesetter's choices do not.
    """
    if normalise(anchor) in normalise(haystack):
        return True
    if any(character.isdigit() for character in anchor):
        return False
    return squashed(anchor) in squashed(haystack)


def extract(document_id: str, cache: dict[str, Extraction]) -> Extraction:
    from app.features.ingestion.parsers.pdfplumber_parser import PdfPlumberParser
    from eval.corpus import load_manifest

    if document_id in cache:
        return cache[document_id]

    record = next((item for item in load_manifest() if item.id == document_id), None)
    extraction = Extraction()
    if record is not None and record.path.exists():
        for page in PdfPlumberParser().parse(record.path):
            extraction.pages[page.page_num] = page.text
    cache[document_id] = extraction
    return extraction


def survives_extraction(question: Question, cache: dict[str, Extraction]) -> bool:
    """The anchor is somewhere in the extracted text of a page the question names.

    Searched across *all* the recorded pages rather than requiring every one, because the
    pages list records everywhere the phrase occurs. One surviving occurrence is enough for
    the fact to be retrievable, which is what is being measured.
    """
    for source in question.sources:
        extraction = extract(source.document, cache)
        for page in source.pages:
            if page in extraction.pages and contains(extraction.pages[page], source.anchor):
                return True
    return False


async def measure(service: object) -> list[Grounded]:
    """Run every answerable question and check what actually reached the model.

    Takes a search service rather than building one, so this runs against the same ingested
    tenant as `eval.production` and the two numbers describe the same corpus.
    """
    cache: dict[str, Extraction] = {}
    results: list[Grounded] = []

    for question in load_questions():
        if question.type == "unanswerable":
            # No anchor to survive and no correct passage to reach. Abstention is what these
            # measure, and only the answer harness can see it.
            continue

        anchors = tuple(source.anchor for source in question.sources)
        grounded = Grounded(question_id=question.id, type=question.type, anchors=anchors)
        grounded.extracted = survives_extraction(question, cache)

        found = await service.search(question.question, limit=PASSAGES)  # type: ignore[attr-defined]
        for rank, hit in enumerate(found.hits, start=1):
            if any(contains(hit.text, anchor) for anchor in anchors):
                grounded.in_context = True
                grounded.context_rank = rank
                break

        results.append(grounded)
        print(
            f"{question.id:<26} {question.type:<15} "
            f"extracted={'yes' if grounded.extracted else 'NO '} "
            f"in_context={'yes' if grounded.in_context else 'NO '} "
            f"rank={grounded.context_rank or '-'}",
            flush=True,
        )
    return results


def report(results: list[Grounded]) -> dict[str, object]:
    def rate(items: list[Grounded], attribute: str) -> float:
        if not items:
            return 0.0
        return round(sum(getattr(item, attribute) for item in items) / len(items), 4)

    kinds = sorted({item.type for item in results})
    tables = [item for item in results if item.type == "table"]

    return {
        "questions": len(results),
        "passages_per_answer": PASSAGES,
        "extraction_rate": rate(results, "extracted"),
        "context_rate": rate(results, "in_context"),
        "by_type": {
            kind: {
                "extraction": rate([i for i in results if i.type == kind], "extracted"),
                "context": rate([i for i in results if i.type == kind], "in_context"),
            }
            for kind in kinds
        },
        # The Docling business case, as a count rather than an opinion. Every one of these
        # is a question no reranker, no larger context and no better model can ever answer.
        "docling": {
            "table_questions": len(tables),
            "anchors_lost_in_extraction": sum(not item.extracted for item in tables),
            "lost": [item.question_id for item in tables if not item.extracted],
        },
        "lost_in_extraction": [item.question_id for item in results if not item.extracted],
        "found_but_not_in_context": [
            item.question_id for item in results if item.extracted and not item.in_context
        ],
    }


def verdict(summary: dict[str, object]) -> list[str]:
    docling = summary["docling"]
    assert isinstance(docling, dict)
    lost = docling["anchors_lost_in_extraction"]

    lines = [
        f"extraction  {summary['extraction_rate']:.0%}  the anchor survives parsing",
        f"context     {summary['context_rate']:.0%}  it reaches the model in the top "
        f"{summary['passages_per_answer']}",
        "",
    ]
    if lost:
        lines += [
            f"DOCLING IS JUSTIFIED: pdfplumber loses {lost} of "
            f"{docling['table_questions']} table anchors.",
            f"  {', '.join(docling['lost'])}",
            "No reranker, no larger context and no better model can answer these.",
        ]
    else:
        lines += [
            f"DOCLING IS NOT JUSTIFIED BY THIS CORPUS: pdfplumber preserves all "
            f"{docling['table_questions']} table anchors.",
            "Whatever is wrong with table answers is downstream of extraction, so adding",
            "Docling would be a dependency with no measured benefit. Re-run this against a",
            "corpus with harder tables before installing it.",
        ]
    return lines


async def ingested_profile() -> object | None:
    """An access profile over whatever tenant already holds the corpus.

    Reusing the ingested tenant rather than provisioning a fresh one, because ingestion is
    forty minutes to produce byte-identical chunks. Returns `None` when nothing has been
    ingested, so the caller can say so rather than reporting 0% and looking like a result.
    """
    from sqlalchemy import text as sql

    from app.core.database import owner_session
    from app.features.auth.access.permissions import CATALOGUE
    from app.features.auth.service import AccessProfile
    from app.features.tenancy.context import TenantContext

    async with owner_session() as session:
        row = (
            await session.execute(
                sql(
                    "SELECT d.tenant_id FROM documents d "
                    "WHERE EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = d.id) LIMIT 1"
                )
            )
        ).first()
        if row is None:
            return None
        label_id = await session.scalar(
            sql("SELECT id FROM access_labels WHERE tenant_id = :t AND is_default"),
            {"t": row.tenant_id},
        )
        user_id = await session.scalar(
            sql("SELECT id FROM users WHERE tenant_id = :t LIMIT 1"), {"t": row.tenant_id}
        )

    return AccessProfile(
        user_id=user_id,
        context=TenantContext.for_tenant(row.tenant_id, [label_id]),
        permissions=frozenset(CATALOGUE),
    )


async def run() -> None:
    from app.core.hardware import PROFILES
    from app.features.retrieval.service import SearchService
    from eval.production import LocalQueryEmbedder, LocalReranker

    profile = await ingested_profile()
    if profile is None:
        print("No ingested corpus found. Run `uv run python -m eval.production` first.")
        return

    # The reranker is part of the shipped path and it is not a detail: F7 measured it
    # moving Recall@8 from 80% to 95%. A ceiling measured without it would describe a
    # product nobody runs, and would understate the identifier questions most of all.
    #
    # Passed explicitly rather than left as `None`, which `SearchService` reads as "build
    # the default" — that is how the first run of this harness silently measured a degraded
    # search against an unreachable TEI.
    service = SearchService(
        profile,  # type: ignore[arg-type]
        embedder=LocalQueryEmbedder(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        reranker=LocalReranker(),  # type: ignore[arg-type]
    )

    results = await measure(service)
    summary = report(results)
    print("\n" + json.dumps(summary, indent=2))
    print()
    for line in verdict(summary):
        print(line)
    REPORT.write_text(
        json.dumps({"summary": summary, "results": [vars(r) for r in results]}, indent=2)
    )


if __name__ == "__main__":
    asyncio.run(run())
