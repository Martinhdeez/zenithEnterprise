"""The official span indices remain exact source coordinates."""

from typing import Any

from eval.contract_nli_trial import build_document
from eval.qasper_trial import paired_paper_bootstrap


def test_contract_labels_map_exact_spans_and_exclude_not_mentioned() -> None:
    text = "Definition. Exception applies."
    raw: dict[str, Any] = {
        "id": 7,
        "text": text,
        "spans": [[0, 11], [11, 12], [12, 30]],
        "annotation_sets": [
            {
                "annotations": {
                    "nda-1": {"choice": "Entailment", "spans": [0, 1, 2]},
                    "nda-2": {"choice": "NotMentioned", "spans": []},
                }
            }
        ],
    }
    labels: dict[str, Any] = {
        "nda-1": {"hypothesis": "A definition applies with an exception."},
        "nda-2": {"hypothesis": "An unrelated claim."},
    }
    paper, counts, blank_spans = build_document(raw, labels)
    assert paper.text == text
    assert paper.questions == 2
    assert counts == {"Entailment": 1, "NotMentioned": 1}
    assert blank_spans == 1
    assert len(paper.cases) == 1
    assert paper.cases[0].label_choice == "Entailment"
    assert [text[start:end] for start, end in paper.cases[0].evidence_options[0]] == [
        "Definition.",
        "Exception applies.",
    ]


def test_bootstrap_uses_document_clusters_and_paired_differences() -> None:
    rows: list[dict[str, object]] = [
        {
            "paper_id": paper_id,
            "routes": {
                "legacy": {"complete_evidence": baseline},
                "grouped_from_legacy": {"complete_evidence": grouped},
                "resegmented_reembedded": {"complete_evidence": baseline},
            },
        }
        for paper_id, baseline, grouped in (
            ("a", False, True),
            ("a", False, True),
            ("b", True, True),
        )
    ]
    result = paired_paper_bootstrap(rows)
    assert result["grouped_from_legacy"]["paper_clusters"] == 2
    assert result["grouped_from_legacy"]["macro_paper_difference"] == 0.5
    assert result["resegmented_reembedded"]["macro_paper_difference"] == 0
