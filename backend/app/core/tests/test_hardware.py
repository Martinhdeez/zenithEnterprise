"""The profile table, and the rule it must never break."""

import pytest

from app.core import hardware
from app.core.hardware import PROFILES, active


def test_every_profile_is_internally_consistent() -> None:
    """A client batch that cannot fit the token budget is a guaranteed 413.

    M0 hit exactly that: TEI at `--max-batch-tokens 2048`, a client sending eight chunks of
    roughly 350 tokens. The two numbers are one setting in two places, and this is the
    assertion that keeps them in step.
    """
    from app.features.ingestion.chunking.chunker import CHARACTERS_PER_TOKEN, TARGET_CHARACTERS

    chunk_tokens = TARGET_CHARACTERS // CHARACTERS_PER_TOKEN

    for profile in PROFILES.values():
        assert profile.max_client_batch_size * chunk_tokens <= profile.max_batch_tokens, (
            f"{profile.name}: a full client batch of target-sized chunks exceeds the "
            "server's token budget, so some requests are guaranteed to fail"
        )
        assert chunk_tokens < profile.max_batch_tokens, (
            f"{profile.name}: a single target-sized chunk exceeds the budget, so no batch "
            "containing it can ever be legal"
        )


def test_ef_search_can_fill_the_candidate_set() -> None:
    """A profile below the candidate count truncates the dense half and says nothing.

    pgvector returns at most `ef_search` rows from one index scan, so `low-spec` at 40
    answered a 50-candidate request with forty rows — 76.3% of the true top-50, and on the
    one profile with no reranker downstream to repair it. Nothing failed, nothing logged;
    fusion simply got a smaller union than it asked for. Swept on the demonstration
    machine, `eval/ef-search.json`.

    Only the floor is asserted. Above it the value stopped changing any result we could
    measure, so this pins the property that matters and leaves the tuning alone.
    """
    from app.features.retrieval.search import CANDIDATES

    for profile in PROFILES.values():
        assert profile.hnsw_ef_search >= CANDIDATES, (
            f"{profile.name}: ef_search {profile.hnsw_ef_search} is below the "
            f"{CANDIDATES} candidates the dense half asks for, so the index cannot return "
            "them all and fusion is handed a truncated union"
        )


def test_low_spec_is_strictly_sequential() -> None:
    """Not a preference. M0 killed TEI twice on this hardware — once by OOM during warm-up,
    once with exit 139 when concurrency was pushed into the server."""
    assert PROFILES["low-spec"].ingestion_concurrency == 1


def test_low_spec_degradations_are_named() -> None:
    """`zenith diagnose` prints these. A customer running without the reranker is losing up
    to 20 points of Recall@8 and must be able to find that out from a diagnostic rather
    than by noticing the answers are worse."""
    disabled = PROFILES["low-spec"].disabled

    assert len(disabled) == 2
    assert any("reranker" in item for item in disabled)
    assert any("OCR" in item for item in disabled)

    # `cpu` has a reranker, so only OCR is missing — and it is missing on *every* profile
    # until an engine is wired, whatever the profile's own flag says. `cpu` and `gpu` both
    # set `ocr=True`, which is a statement about the hardware; the installation's answer is
    # the conjunction with `OCR_IMPLEMENTED`, and reporting the flag alone told an operator
    # their scanned documents would ingest when every one of them was being refused.
    assert PROFILES["cpu"].disabled == [
        "OCR (scanned documents are refused rather than ingested empty)"
    ]


def test_no_profile_claims_ocr_this_build_does_not_have() -> None:
    """The criterion, stated as one assertion: a profile reports OCR as available only when
    an implementation exists.

    The field is `ocr_capable_hardware` rather than `ocr` because the short name was itself
    the problem — `cpu` and `gpu` set it true, and somebody reading `hardware.py` alone would
    conclude scanned documents ingest on them.
    """
    from app.core.hardware import OCR_IMPLEMENTED

    for name, profile in PROFILES.items():
        claims_ocr = not any("OCR" in item for item in profile.disabled)
        assert claims_ocr is (profile.ocr_capable_hardware and OCR_IMPLEMENTED), name


def test_an_unknown_profile_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator who types `CPU2` gets an error naming the valid values, not a silent
    fallback to defaults that will run out of memory at three in the morning."""
    monkeypatch.setattr(hardware.settings, "hardware", "CPU2")

    with pytest.raises(ValueError, match="low-spec"):
        active()
