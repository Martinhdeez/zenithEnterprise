"""The public packet harness uses the production selector and exact spans."""

import csv
import json
from pathlib import Path

from eval.evidence_common import TrialUnit
from eval.packet_case_export import ROUTES, export
from eval.qasper_common import Case, Paper
from eval.qasper_packet_trial import make_hits, packet_routes


async def test_packet_trial_recovers_exception_outside_topk() -> None:
    first = "Residents pay 5%."
    second = "Except nonresidents pay 0%."
    gap = "\n" * 10
    text = first + gap + second
    start = len(first + gap)
    paper = Paper(
        "synthetic-public-case",
        text,
        (Case("q1", "Which rate applies?", (((0, len(first)), (start, len(text))),)),),
        1,
    )
    units = [
        TrialUnit("first", 0, len(first), first),
        TrialUnit("second", start, len(text), second),
    ]
    hits, by_id = make_hits(paper, units)
    ordinary, counter = await packet_routes(
        paper,
        paper.cases[0],
        units[:1],
        hits,
        by_id,
        {"first": 20, "second": 30},
    )
    assert ordinary["complete_evidence"] is True
    assert ordinary["selected_units"] == 2
    assert ordinary["rendered_tokens"] == 50
    assert counter["complete_evidence"] is True
    assert counter["counterevidence_candidates"] == 0


def test_case_export_keeps_paired_outcomes_without_source_text(tmp_path: Path) -> None:
    outcome = {"complete_evidence": True, "evidence_recall": 1.0}
    report: dict[str, object] = {
        "trial_version": "test-v1",
        "rows": [
            {
                "paper_id": "public-1",
                "question_id": "q1",
                "label_choice": None,
                "routes": {route: outcome for route in ROUTES},
                "source_text": "private passage must not export",
            }
        ],
    }
    csv_path = tmp_path / "cases.csv"
    manifest_path = tmp_path / "manifest.json"
    export(report, csv_path, manifest_path)
    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["dense_topk_complete_evidence"] == "True"
    assert "private passage" not in csv_path.read_text(encoding="utf-8")
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {"trial_version": "test-v1"}
