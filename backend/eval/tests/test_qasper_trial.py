"""Benchmark rules are fixed independently of model scores."""

from eval.lossless_trial import TrialUnit
from eval.qasper_trial import Case, build_paper, score_case, units_for_paper


def test_rendered_evidence_maps_to_exact_source_offsets() -> None:
    paragraph = "A definition with an exception that must stay visible. " * 3
    raw = {
        "full_text": [{"section_name": "Terms", "paragraphs": [paragraph, "A second passage."]}],
        "qas": [
            {
                "question_id": "question-1",
                "question": "Which terms apply?",
                "answers": [
                    {"answer": {"unanswerable": False, "evidence": [paragraph]}},
                    {"answer": {"unanswerable": False, "evidence": ["A second passage."]}},
                ],
            }
        ],
    }
    paper = build_paper("paper-1", raw)
    assert paper.text.startswith("# Terms\n")
    assert len(paper.cases) == 1
    assert len(paper.cases[0].evidence_options) == 2
    for option in paper.cases[0].evidence_options:
        for start, end in option:
            assert paper.text[start:end] in {paragraph, "A second passage."}
    legacy, groups = units_for_paper(paper)
    assert legacy and groups
    assert "".join(unit.text for unit in groups) == paper.text


def test_ambiguous_or_unanswerable_annotation_does_not_become_gold() -> None:
    repeated = "The same passage."
    raw = {
        "full_text": [{"section_name": "", "paragraphs": [repeated, repeated, "Unique."]}],
        "qas": [
            {
                "question_id": "q",
                "question": "What is unique?",
                "answers": [
                    {"answer": {"unanswerable": False, "evidence": [repeated]}},
                    {"answer": {"unanswerable": True, "evidence": ["Unique."]}},
                    {"answer": {"unanswerable": False, "evidence": ["Unique."]}},
                ],
            }
        ],
    }
    paper = build_paper("paper-2", raw)
    assert len(paper.cases[0].evidence_options) == 1
    start, end = paper.cases[0].evidence_options[0][0]
    assert paper.text[start:end] == "Unique."


def test_complete_evidence_uses_a_single_human_annotation() -> None:
    case = Case("q", "Question", (((0, 5), (10, 15)), ((20, 25),)))
    ranked = [
        TrialUnit("a", 0, 5, "first"),
        TrialUnit("b", 20, 25, "other"),
    ]
    score = score_case(case, ranked, {"a": 2, "b": 2})
    assert score["complete_evidence"] is True
    assert score["evidence_recall"] == 1.0
    assert score["top1_intersects_evidence"] is True
