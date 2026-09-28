"""The descriptive join must retain both studies' separate provenance."""

import hashlib
import json
from pathlib import Path

import pytest

from eval.qasper_extension_jev import combine


def _save(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_combined_manifest_has_aggregate_counts_and_separate_approvals(tmp_path: Path) -> None:
    baseline, output = tmp_path / "baseline", tmp_path / "extension"
    baseline.mkdir()
    output.mkdir()
    shared = {
        "dataset_sha256": "dataset",
        "archive_sha256": "archive",
        "candidate_policy": "same candidates",
        "tei_model": "tei",
        "jev_model": "jev",
        "jev_rubric_id": "noul",
        "jev_rubric_hash": "rubric",
    }
    components = [
        (
            baseline,
            "heldout-240-frozen-manifest.json",
            "heldout-240-jev-ledger.json",
            240,
            1916,
            13550155,
            10000,
            5.0,
        ),
        (output, "manifest.json", "ledger.json", 176, 1396, 9869151, 1408, 1.0),
    ]
    hashes: list[str] = []
    for directory, manifest_name, ledger_name, papers, calls, tokens, cap, usd in components:
        manifest_path = directory / manifest_name
        base_calls, extra_calls = divmod(calls, papers)
        _save(
            manifest_path,
            {
                **shared,
                "trial_id": f"trial-{papers}",
                "rows": [
                    {
                        "paper_id": f"paper-{papers}-{i}",
                        "candidates": [{}] * (base_calls + (i < extra_calls)),
                    }
                    for i in range(papers)
                ],
                "planned_calls": calls,
                "conservative_input_tokens": tokens,
                "approved_additional_calls": cap,
                "approved_additional_usd": usd,
            },
        )
        manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        hashes.append(manifest_hash)
        _save(
            directory / ledger_name,
            {
                "manifest_sha256": manifest_hash,
                "reserved": [f"paper-{papers}-{i}" for i in range(papers)],
                "rows": [],
            },
        )

    combine(baseline, output)
    combined_path = output / "combined-manifest.json"
    combined = json.loads(combined_path.read_text(encoding="utf-8"))
    ledger = json.loads((output / "combined-ledger.json").read_text(encoding="utf-8"))
    assert combined["planned_calls"] == 3312
    assert len(ledger["reserved"]) == 416
    assert combined["conservative_input_tokens"] == 23419306
    assert len(combined["rows"]) == 416
    assert "approved_additional_calls" not in combined
    assert "approved_additional_usd" not in combined
    assert [item["manifest_sha256"] for item in combined["component_trials"]] == hashes
    assert [item["approved_additional_calls"] for item in combined["component_trials"]] == [
        10000,
        1408,
    ]
    assert [item["approved_additional_usd"] for item in combined["component_trials"]] == [
        5.0,
        1.0,
    ]
    assert ledger["manifest_sha256"] == hashlib.sha256(combined_path.read_bytes()).hexdigest()

    # Reusing saved predictions remains safe; changing a judge contract must not join.
    extension_path = output / "manifest.json"
    changed = json.loads(extension_path.read_text(encoding="utf-8"))
    changed["jev_rubric_hash"] = "different"
    _save(extension_path, changed)
    changed_ledger = json.loads((output / "ledger.json").read_text(encoding="utf-8"))
    changed_ledger["manifest_sha256"] = hashlib.sha256(extension_path.read_bytes()).hexdigest()
    _save(output / "ledger.json", changed_ledger)
    with pytest.raises(ValueError, match="different datasets, candidates, or judges"):
        combine(baseline, output)
