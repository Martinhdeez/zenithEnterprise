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
    an implementation exists."""
    from app.core.hardware import OCR_IMPLEMENTED

    for name, profile in PROFILES.items():
        claims_ocr = not any("OCR" in item for item in profile.disabled)
        assert claims_ocr is (profile.ocr and OCR_IMPLEMENTED), name


def test_an_unknown_profile_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator who types `CPU2` gets an error naming the valid values, not a silent
    fallback to defaults that will run out of memory at three in the morning."""
    monkeypatch.setattr(hardware.settings, "hardware", "CPU2")

    with pytest.raises(ValueError, match="low-spec"):
        active()
