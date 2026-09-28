"""Matched public direct/hybrid pilot through application-role PostgreSQL and local TEI.

Opt in with ZENITH_RUN_PUBLIC_DIRECT_HYBRID=1. The fixed fixture has agent-reviewed
labels and test embeddings, so this diagnoses routing and coverage, not model promotion.
"""

import hashlib
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.core.database import owner_session
from app.core.hardware import PROFILES
from app.features.retrieval.reranker import TeiReranker
from app.features.retrieval.service import SearchResult, SearchService
from app.features.retrieval.tests.test_search import profile_for, seed
from conftest import Account, WorkingEmbedder
from eval.judge_comparison import ndcg

FIXTURE = Path(__file__).parent / "fixtures/evidence-v3-public-v1.json"


def _metrics(
    result: SearchResult, grades: list[int], index: dict[str, int], ids: list[str]
) -> dict[str, object]:
    order = [index[hit.text] for hit in result.hits]
    relevant = {position for position, grade in enumerate(grades) if grade >= 3}
    return {
        "ranked_candidate_ids": [ids[position] for position in order],
        "best_at_1": bool(order) and grades[order[0]] == max(grades),
        "ndcg_at_8": ndcg(grades, order),
        "relevant_recall_at_8": len(relevant.intersection(order)) / len(relevant),
        "latency_ms": result.took_ms,
        "assessed_candidates": len(result.assessment.requested_ids) if result.assessment else 0,
        "returned_candidates": len(order),
        "provider": result.assessment.provider if result.assessment else None,
        "model": result.assessment.reported_model if result.assessment else None,
        "receipt": {
            "strategy": result.receipt.strategy,
            "execution_status": result.receipt.execution_status,
            "eligible_units": result.receipt.eligible_units,
            "assessed_units": result.receipt.assessed_units,
            "manifest_assessment_complete": result.receipt.manifest_assessment_complete,
        }
        if result.receipt
        else None,
    }


async def test_public_direct_vs_hybrid_same_tei_and_scope(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.environ.get("ZENITH_RUN_PUBLIC_DIRECT_HYBRID") != "1":
        pytest.skip("public direct/hybrid pilot requires explicit opt-in")
    monkeypatch.setattr(settings, "direct_enabled", True)
    raw = FIXTURE.read_bytes()
    fixture = json.loads(raw)
    passages = [(item["text"], position + 1) for position, item in enumerate(fixture["candidates"])]
    document = await seed(account.tenant_id, account.default_label, passages)
    profile = await profile_for(account)
    index = {item["text"]: position for position, item in enumerate(fixture["candidates"])}
    ids = [item["id"] for item in fixture["candidates"]]
    tei = TeiReranker(
        url=os.environ.get("ZENITH_PUBLIC_TEI_URL", "http://127.0.0.1:18083"),
        profile=PROFILES["cpu"],
    )
    model = await tei.model_identity()
    rows: list[dict[str, object]] = []
    for query in fixture["queries"]:
        hybrid = await SearchService(
            profile,
            embedder=WorkingEmbedder(),  # type: ignore[arg-type]
            hardware=PROFILES["cpu"],
            reranker=tei,
        ).search(query["text"], documents=[document], mode="hybrid")
        direct = await SearchService(
            profile,
            embedder=WorkingEmbedder(),  # type: ignore[arg-type]
            hardware=PROFILES["cpu"],
            reranker=tei,
        ).search(query["text"], documents=[document], mode="direct")
        assert direct.receipt is not None and direct.receipt.manifest_assessment_complete
        assert hybrid.receipt is not None and not hybrid.receipt.manifest_assessment_complete
        rows.append(
            {
                "query_id": query["id"],
                "hybrid": _metrics(hybrid, query["grades"], index, ids),
                "direct": _metrics(direct, query["grades"], index, ids),
            }
        )
    target = fixture["candidates"][0]["text"]
    async with owner_session() as session:
        await session.execute(
            text(
                "DELETE FROM chunk_embeddings WHERE chunk_id IN "
                "(SELECT id FROM chunks WHERE document_id = :document AND text = :target)"
            ),
            {"document": document, "target": target},
        )
    probe = "Does increasing thermal energy eliminate dissolved Pb?"
    hybrid_probe = await SearchService(
        profile,
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        reranker=tei,
    ).search(probe, documents=[document], mode="hybrid")
    direct_probe = await SearchService(
        profile,
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        reranker=tei,
    ).search(probe, documents=[document], mode="direct")
    assert direct_probe.receipt is not None and direct_probe.receipt.manifest_assessment_complete
    assert any(hit.text == target for hit in direct_probe.hits)
    missing_vector_probe = {
        "query_kind": "agent-authored public paraphrase; no reviewed grade label",
        "query": probe,
        "target_candidate_id": ids[0],
        "target_in_hybrid": any(hit.text == target for hit in hybrid_probe.hits),
        "target_in_direct": any(hit.text == target for hit in direct_probe.hits),
        "hybrid_candidate_count": len(hybrid_probe.assessment.requested_ids)
        if hybrid_probe.assessment
        else 0,
        "direct_window_count": len(direct_probe.assessment.requested_ids)
        if direct_probe.assessment
        else 0,
    }
    report = {
        "fixture_id": fixture["id"],
        "fixture_sha256": hashlib.sha256(raw).hexdigest(),
        "label_provenance": fixture["label_provenance"],
        "embedding_note": (
            "Test-seeded vectors and deterministic WorkingEmbedder; not a real embedding model"
        ),
        "source_scope": "One ready document with eight public source-grounded paraphrase chunks",
        "tei_model": model,
        "tei_endpoint": os.environ.get("ZENITH_PUBLIC_TEI_URL", "http://127.0.0.1:18083"),
        "rows": rows,
        "missing_vector_probe": missing_vector_probe,
    }
    output = Path(os.environ.get("ZENITH_PUBLIC_DIRECT_REPORT", "public-direct-hybrid.json"))
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
