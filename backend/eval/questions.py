"""The question set, and what makes it trustworthy.

An unverified question set measures the model that drafted it, not the retrieval system.
So every question records the exact phrase its answer comes from, the document it is in,
and every page containing that phrase — and `tests/test_questions.py` re-derives all of it
from the extracted text.

That turns "hand-verified" from a claim in a commit message into a property the suite
re-checks on every run, including after the corpus is re-fetched.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

QUESTIONS = Path(__file__).resolve().parent / "questions.toml"

QuestionType = Literal["factual", "cross-document", "table", "unanswerable"]

# The headline number is computed over these two only.
#
# `table` is expected to score near zero: the tracer bullet runs pdfplumber with no Docling
# routing, so tables are flattened by design. `unanswerable` has no correct passage at all,
# which makes recall undefined — averaging it in scores correct abstention as failure.
#
# Both are measured and reported, as baselines their own later work has to beat. Neither
# belongs in the number that decides whether retrieval works.
HEADLINE_TYPES: frozenset[str] = frozenset({"factual", "cross-document"})


@dataclass(frozen=True, slots=True)
class Source:
    document: str
    anchor: str
    pages: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Question:
    id: str
    type: QuestionType
    question: str
    sources: tuple[Source, ...]

    @property
    def documents(self) -> tuple[str, ...]:
        return tuple(source.document for source in self.sources)

    @property
    def counts_towards_headline(self) -> bool:
        return self.type in HEADLINE_TYPES


def load_questions() -> list[Question]:
    raw: dict[str, Any] = tomllib.loads(QUESTIONS.read_text())
    return [
        Question(
            id=entry["id"],
            type=entry["type"],
            question=entry["question"],
            sources=tuple(
                Source(
                    document=source["document"],
                    anchor=source["anchor"],
                    pages=tuple(source["pages"]),
                )
                for source in entry.get("source", [])
            ),
        )
        for entry in raw["question"]
    ]
