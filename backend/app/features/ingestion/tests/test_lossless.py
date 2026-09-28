"""R1 source-coverage and versioning invariants on disposable extracted text."""

import hashlib
import json
from uuid import uuid4

import httpx
import pytest

from app.features.ingestion.chunking.boundary import assess_boundaries
from app.features.ingestion.chunking.lossless import (
    BoundaryAssessment,
    Kind,
    Source,
    atomic_spans,
    boundary_id,
    overlapping_chunk_ids,
    structural_groups,
    validate_groups,
    validate_spans,
)
from app.features.retrieval.judging.jev import JevJudge, JevQuota, ProcessingPolicy, Purpose
from app.features.retrieval.judging.rubrics import BOUNDARY_NOUL, Formulation


def source(text: str, *, extraction_version: str = "parser-v1") -> Source:
    return Source(
        uuid4(),
        2,
        text,
        hashlib.sha256(text.encode()).hexdigest(),
        "pdfplumber",
        extraction_version,
    )


def test_bilingual_codepoints_crlf_and_structure_are_lossless() -> None:
    original = (
        "# Obligaciones / Duties\r\n"
        "1. La tasa es 3.14 en https://example.org/a.b.\r\n"
        "2. Notifique al señor García.\r\n"
        "```python\r\ncode = 'é 😀'\r\n```\r\n"
        "| Concepto | Importe |\r\n| Agua | 20 € |\r\n"
    )
    page = source(original)
    spans = atomic_spans(page, max_chars=80)
    assert "".join(item.text for item in spans) == original
    assert [item.kind for item in spans[:3]] == [Kind.HEADING, Kind.LIST, Kind.LIST]
    assert Kind.CODE in [item.kind for item in spans]
    assert Kind.TABLE in [item.kind for item in spans]
    groups = structural_groups(page, spans, target_chars=70, max_chars=100)
    assert "".join(item.text for item in groups) == original
    assert all(item.text == original[item.start : item.end] for item in groups)


def test_oversized_atomic_line_forces_mapped_splits_without_a_loop() -> None:
    page = source("x" * 501)
    spans = atomic_spans(page, max_chars=90)
    groups = structural_groups(page, spans, target_chars=140, max_chars=180)
    assert len(spans) == 6
    assert all(item.forced_split for item in spans)
    assert all(len(item.text) <= 180 for item in groups)
    assert "".join(item.text for item in groups) == page.text


def test_failed_boundary_falls_back_and_version_change_invalidates_ids() -> None:
    document_id = uuid4()
    page = Source(document_id, None, "Alpha\nBeta\nGamma\n", "file-sha", "text", "v1")
    spans = atomic_spans(page)
    choice = boundary_id(spans[0], spans[1])
    unavailable = BoundaryAssessment(
        choice,
        page.identity,
        None,
        "unavailable",
        "jev",
        "jev-1.13.0",
        "zenith-boundary-noul-v1",
        None,
    )
    baseline = structural_groups(page, spans, target_chars=8, max_chars=12)
    fallback = structural_groups(
        page, spans, target_chars=8, max_chars=12, assessments=(unavailable,)
    )
    assert fallback == baseline
    changed = Source(document_id, None, page.text, "file-sha", "text", "v2")
    assert atomic_spans(changed)[0].id != spans[0].id
    with pytest.raises(ValueError, match="source mapping"):
        validate_spans(changed, spans)


def test_usable_boundary_is_only_a_cut_preference_and_bad_mapping_is_rejected() -> None:
    page = source("Heading\nFirst clause\nException applies\n")
    spans = atomic_spans(page)
    assessment = BoundaryAssessment(
        boundary_id(spans[1], spans[2]),
        page.identity,
        1.0,
        "assessed",
        "fake",
        "v1",
        "boundary-v1",
        "input-sha",
    )
    groups = structural_groups(
        page, spans, target_chars=18, max_chars=30, assessments=(assessment,)
    )
    assert "".join(item.text for item in groups) == page.text
    assert groups[0].end == spans[1].end
    with pytest.raises(ValueError, match="coverage"):
        validate_groups(page, groups[1:])
    with pytest.raises(ValueError, match="probability"):
        BoundaryAssessment(
            assessment.boundary_id,
            page.identity,
            float("nan"),
            "assessed",
            "fake",
            None,
            None,
            None,
        )


def test_legacy_overlap_maps_coordinates_without_double_counting() -> None:
    page = source("A" * 100 + "B" * 100)
    spans = atomic_spans(page, max_chars=100)
    groups = structural_groups(page, spans, target_chars=100, max_chars=100)
    first, second = uuid4(), uuid4()
    legacy = ((first, 0, 120), (second, 80, 200))
    assert overlapping_chunk_ids(groups[0], legacy) == (first, second)
    assert overlapping_chunk_ids(groups[1], legacy) == (first, second)
    assert sum(len(group.text) for group in groups) == 200


async def test_boundary_adapter_uses_separate_purpose_and_versioned_noul() -> None:
    page = source("Public heading\nPublic rule\nPublic exception\n")
    spans = atomic_spans(page)
    calls: list[dict[str, object]] = []
    authorizations: list[tuple[str, Purpose]] = []

    async def authorize(question: str, candidate: object, purpose: Purpose) -> bool:
        authorizations.append((question, purpose))
        return purpose is Purpose.SEGMENTATION and "Public" in candidate.text  # type: ignore[attr-defined]

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {"contribution": {"type": "noul", "noul": 0.8}},
                "usage": {"input_tokens": 30, "output_tokens": 5},
            },
        )

    judge = JevJudge(
        api_key="synthetic-test-key",
        formulation=Formulation.NOUL,
        rubric=BOUNDARY_NOUL,
        purpose=Purpose.SEGMENTATION,
        policy=ProcessingPolicy(segmentation=True),
        authorize=authorize,
        quota=JevQuota(2, 100_000),
        transport=httpx.MockTransport(handler),
    )
    try:
        proposed = await assess_boundaries(page, spans, judge, max_boundaries=1)
    finally:
        await judge.aclose()
    assert len(calls) == 1 and len(authorizations) == 2
    assert all(purpose is Purpose.SEGMENTATION for _, purpose in authorizations)
    assert calls[0]["questions"]["contribution"] == BOUNDARY_NOUL.question()  # type: ignore[index]
    assert "<BOUNDARY>" in calls[0]["state"]["candidate_passage"]  # type: ignore[index]
    assert proposed[0].status == "assessed" and proposed[0].probability == 0.8
    assert proposed[0].rubric_id == BOUNDARY_NOUL.id and proposed[0].input_fingerprint
    assert proposed[1].status == "unavailable" and proposed[1].failure_code == "boundary_budget"


async def test_boundary_egress_denial_and_malformed_response_fall_back() -> None:
    page = source("Private rule\nPrivate qualification\n")
    spans = atomic_spans(page)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"model": "wrong", "answers": {}})

    async def denied(question: str, candidate: object, purpose: Purpose) -> bool:
        return False

    judge = JevJudge(
        api_key="synthetic-test-key",
        formulation=Formulation.NOUL,
        rubric=BOUNDARY_NOUL,
        purpose=Purpose.SEGMENTATION,
        policy=ProcessingPolicy(segmentation=True),
        authorize=denied,
        quota=JevQuota(2, 100_000),
        transport=httpx.MockTransport(handler),
    )
    try:
        assessments = await assess_boundaries(page, spans, judge)
    finally:
        await judge.aclose()
    assert calls == 0 and assessments[0].status == "unavailable"
    assert structural_groups(page, spans, target_chars=20, max_chars=30, assessments=assessments)

    async def permitted(question: str, candidate: object, purpose: Purpose) -> bool:
        return True

    judge = JevJudge(
        api_key="synthetic-test-key",
        formulation=Formulation.NOUL,
        rubric=BOUNDARY_NOUL,
        purpose=Purpose.SEGMENTATION,
        policy=ProcessingPolicy(segmentation=True),
        authorize=permitted,
        quota=JevQuota(2, 100_000),
        transport=httpx.MockTransport(handler),
    )
    try:
        assessments = await assess_boundaries(page, spans, judge)
    finally:
        await judge.aclose()
    assert calls == 1 and assessments[0].status == "unavailable"
    assert assessments[0].failure_code == "model_mismatch"
