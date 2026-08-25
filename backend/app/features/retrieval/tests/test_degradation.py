"""The sentences a customer reads when part of the search is not working.

These are the only strings in the product that appear on screen *because something broke*, in
front of whoever is being shown it. They used to be written for the person who built it —
`semantic search unavailable (ConnectError); lexical only` — so they get a test that is about
the reader rather than about the mechanism.
"""

import re

import pytest

from app.features.retrieval.degradation import RERANKING_UNAVAILABLE, SEMANTIC_UNAVAILABLE

SENTENCES = [SEMANTIC_UNAVAILABLE, RERANKING_UNAVAILABLE]

#: Anything shaped like a Python or HTTP exception class: `ConnectError`, `ReadTimeout`,
#: `HTTPStatusError`. Two capitalised words run together is not how English is written and is
#: exactly how these class names are.
CLASS_NAME = re.compile(r"\b[A-Z][a-z]+[A-Z][a-zA-Z]*\b")

#: Words that name our machinery rather than the reader's experience. "Fused order" is an
#: algorithm, "lexical" is a retrieval term, "circuit" is a resilience pattern. Each was on
#: screen at some point.
JARGON = ["fused", "lexical", "circuit", "cross-encoder", "rrf", "embedding", "vector"]


@pytest.mark.parametrize("sentence", SENTENCES)
def test_no_exception_class_names_reach_the_reader(sentence: str) -> None:
    assert not CLASS_NAME.search(sentence), (
        "an exception class name is on screen; it belongs in the log, where somebody who "
        "can act on it will look"
    )


@pytest.mark.parametrize("sentence", SENTENCES)
@pytest.mark.parametrize("word", JARGON)
def test_no_internal_vocabulary_reaches_the_reader(sentence: str, word: str) -> None:
    assert word not in sentence.lower()


@pytest.mark.parametrize("sentence", SENTENCES)
def test_each_sentence_says_what_is_different_about_these_results(sentence: str) -> None:
    """Not merely that something is unavailable.

    "Search is degraded" tells a reader to distrust the page without telling them how far.
    Each sentence has to name the consequence — wording matters more, or the order is less
    refined — because that is the only part they can act on.
    """
    assert "—" in sentence, "the sentence names a component but not its consequence"
    _, consequence = sentence.split("—", 1)
    assert len(consequence.split()) >= 5


def test_reranking_reassures_about_what_did_not_change() -> None:
    """The more damaging misunderstanding is "these results are wrong".

    Reranking changes the order of the results, not which documents were found. A warning
    that does not say so invites somebody to discard a correct answer.
    """
    assert "same documents" in RERANKING_UNAVAILABLE
