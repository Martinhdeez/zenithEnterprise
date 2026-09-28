"""SciFact sample selection and public-only preflight without Jev calls."""

import hashlib
import json
from pathlib import Path

import pytest

from eval import scifact_support_trial as trial


def test_balanced_selection_requires_pinned_public_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims_path = tmp_path / "claims_dev.jsonl"
    corpus_path = tmp_path / "corpus.jsonl"
    claims = [
        {
            "id": 1,
            "claim": "The rate is 5%.",
            "evidence": {"10": [{"label": "SUPPORT", "sentences": [0]}]},
        },
        {
            "id": 2,
            "claim": "The rate is 8%.",
            "evidence": {"10": [{"label": "CONTRADICT", "sentences": [0]}]},
        },
    ]
    corpus = [{"doc_id": 10, "title": "Public source", "abstract": ["The rate is 5%."]}]
    claims_path.write_text("".join(json.dumps(row) + "\n" for row in claims), encoding="utf-8")
    corpus_path.write_text("".join(json.dumps(row) + "\n" for row in corpus), encoding="utf-8")
    monkeypatch.setattr(
        trial, "CLAIMS_SHA256", hashlib.sha256(claims_path.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(
        trial, "CORPUS_SHA256", hashlib.sha256(corpus_path.read_bytes()).hexdigest()
    )
    selected = trial.select(claims_path, corpus_path, per_label=1)
    assert [row["label"] for row in selected] == ["SUPPORT", "CONTRADICT"]
    assert all("The rate is 5%." in str(row["rendered"]) for row in selected)
    claims_path.write_text(claims_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        trial.select(claims_path, corpus_path)
