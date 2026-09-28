"""Public live comparison selection is fixed before scoring."""

from eval.qasper_judge_trial import selected_cases
from eval.qasper_trial import Case, Paper


def test_selects_one_hash_ranked_question_per_distinct_paper() -> None:
    papers = tuple(
        Paper(
            str(index),
            "public source",
            (
                Case(f"{index}-b", "b", (((0, 6),),)),
                Case(f"{index}-a", "a", (((0, 6),),)),
            ),
            2,
        )
        for index in range(43)
    )
    chosen = selected_cases(papers)
    assert len(chosen) == 40
    assert len({paper.id for paper, _ in chosen}) == 40
    assert chosen == selected_cases(tuple(reversed(papers)))
