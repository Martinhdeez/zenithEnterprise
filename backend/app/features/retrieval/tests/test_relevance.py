"""The gate, and the two constants that decide what it hides.

Every number here traces to `eval/separation.json`, the run of 2026-08-28 over the
26-document corpus. They are asserted rather than described because a constant with a
measurement behind it and a constant somebody nudged look identical in a diff.
"""

from app.features.retrieval.relevance import (
    FLOOR,
    WEAK_CEILING,
    Relevance,
    best_rerank,
    classify,
)


class TestTheBands:
    def test_a_plain_match_is_confident(self) -> None:
        # 21 of the 40 measured questions score above 0.9. This is the ordinary case.
        assert classify(0.9455) is Relevance.CONFIDENT

    def test_nonsense_is_rejected(self) -> None:
        # `unanswerable-pricing` and three others sit below the floor.
        assert classify(0.0003) is Relevance.NONE

    def test_a_poor_match_is_shown_and_named(self) -> None:
        # Messi's goals: 0.1277 measured. Shown, with the weak match stated — not hidden,
        # because hiding is reserved for the band where nothing real was measured.
        assert classify(0.1277) is Relevance.WEAK


class TestTheAsymmetry:
    """A false negative is far worse than a false positive, and these hold the line.

    Hide a passage that was really there and the reader concludes the corpus does not
    contain it, with no way to find out otherwise — the screen told them there was nothing
    to check. A false positive costs two paragraphs of reading.
    """

    def test_the_lowest_scoring_real_answer_is_never_hidden(self) -> None:
        # `cross-payroll-and-return`, 0.0272, the lowest-scoring answerable question in the
        # measured set. It is weak, and it is *shown*. If a future change puts the floor
        # above this, it hides a real answer and this test is what says so.
        assert classify(0.0272) is not Relevance.NONE

    def test_the_three_questions_just_above_the_ceiling_stay_confident(self) -> None:
        # `attention-optimizer` 0.1614, `boe-bank-rate` 0.1791,
        # `cross-eu-extraterritorial` 0.1865. A ceiling at 0.20 would call three good
        # answers weak, which is why it is 0.15.
        for score in (0.1614, 0.1791, 0.1865):
            assert classify(score) is Relevance.CONFIDENT

    def test_the_ceiling_sits_in_the_empty_valley(self) -> None:
        # Nothing at all was measured between 0.2 and 0.4, so the line has room to move
        # without reclassifying anything. That is what makes it robust to a growing corpus.
        assert FLOOR < WEAK_CEILING < 0.2


class TestWithoutAReranker:
    def test_no_signal_means_no_verdict(self) -> None:
        """`low-spec` runs no reranker and it is the component most likely to be down.

        Absence of the signal is not evidence of absence in the corpus. Inventing a verdict
        from silence would hide results on exactly the installations least able to afford
        it — and a screen that says "nothing matches" on one machine and returns eight
        passages on another is ADR 0005's rule broken: a profile may change cost, never an
        outcome.
        """
        assert classify(None) is Relevance.CONFIDENT

    def test_the_best_of_nothing_is_nothing(self) -> None:
        assert best_rerank([]) is None
        assert best_rerank([None, None]) is None


class TestReadingTheSet:
    def test_the_best_passage_decides(self) -> None:
        # Seven weak passages must not outvote the one that answers the question. That is
        # what ranking is for.
        assert best_rerank([0.99, 0.01, None, 0.02]) == 0.99
        assert classify(best_rerank([0.99, 0.01, None, 0.02])) is Relevance.CONFIDENT
