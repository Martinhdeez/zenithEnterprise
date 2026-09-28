"""Held-out preparation discards labels; scoring consumes a frozen prediction."""

# pyright: reportPrivateUsage=false

import hashlib
import json
from pathlib import Path

import pytest

from eval import qasper_heldout_jev as trial
from eval.evidence_common import TrialUnit
from eval.qasper_common import Case


def test_binary_rank_metrics_and_zero_candidate_hit() -> None:
    case = Case("q", "question", (((10, 20),),))
    irrelevant = TrialUnit("a", 0, 9, "a")
    relevant = TrialUnit("b", 10, 20, "b")
    assert trial._ranking_metrics(case, [relevant, irrelevant])["ndcg_at_8"] == 1.0
    assert trial._ranking_metrics(case, [irrelevant, relevant])["mrr_at_8"] == 0.5
    assert trial._ranking_metrics(case, [irrelevant])["ndcg_at_8"] is None


def test_unlabeled_projection_and_later_gold_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paragraph = "The controller shall implement technical measures."
    paper = {
        "full_text": [{"section_name": "Rule", "paragraphs": [paragraph]}],
        "qas": [
            {
                "question_id": "public-question-1",
                "question": "What must the controller implement?",
                "answers": [{"answer": {"unanswerable": False, "evidence": [paragraph]}}],
            }
        ],
    }
    assert trial._unlabeled_paper("public-paper-1", paper).cases == ()
    source = tmp_path / "test.json"
    source.write_text(json.dumps({"public-paper-1": paper}), encoding="utf-8")
    monkeypatch.setattr(trial, "TEST_SHA256", hashlib.sha256(source.read_bytes()).hexdigest())
    manifest = tmp_path / "manifest.json"
    trial._save(
        manifest,
        {
            "trial_id": trial.TRIAL_ID,
            "rows": [
                {
                    "paper_id": "public-paper-1",
                    "question_id": "public-question-1",
                    "tei_order": [0],
                    "tei_elapsed_ms": 10,
                    "candidates": [
                        {
                            "unit_id": "legacy:0",
                            "start": len("# Rule\n"),
                            "end": len("# Rule\n") + len(paragraph),
                            "text": paragraph,
                            "rendered_tokens": 20,
                        }
                    ],
                }
            ],
        },
    )
    ledger = tmp_path / "ledger.json"
    trial._save(
        ledger,
        {
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "reserved": ["public-question-1"],
            "rows": [
                {
                    "question_id": "public-question-1",
                    "status": "assessed",
                    "order": [0],
                    "elapsed_ms": 30,
                    "input_tokens": 10,
                    "output_tokens": 1,
                }
            ],
        },
    )
    cases_csv = tmp_path / "cases.csv"
    result = trial.score(source, manifest, ledger, tmp_path / "scored.json", cases_csv)
    assert result["human_evidence_addressable"] == 1
    assert result["additional_calls_reserved"] == 1
    assert result["tei_rerank_p50_ms"] == 10
    assert result["jev_rerank_p50_ms"] == 30
    assert result["tei_complete"] == result["jev_with_tei_fallback_complete"] == 1.0
    assert "public-question-1" in cases_csv.read_text(encoding="utf-8")
    assert paragraph not in cases_csv.read_text(encoding="utf-8")
