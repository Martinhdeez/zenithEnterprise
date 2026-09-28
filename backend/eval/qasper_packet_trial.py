"""Public QASPER top-k versus opt-in packet selection on fixed local candidates.

The production packet selector runs with in-memory lookups over the identical
legacy chunks, so no private database or external model receives source text.
Candidate embedding and reranking use the pinned local GPU TEI services.
"""

import argparse
import asyncio
import hashlib
import json
import random
import re
import statistics
from pathlib import Path
from typing import cast
from unittest.mock import patch
from uuid import NAMESPACE_URL, UUID, uuid5

from app.core.hardware import PROFILES
from app.features.generation.answering import packet
from app.features.retrieval.reranker import TeiReranker
from app.features.retrieval.search import Hit
from app.features.tenancy.context import TenantContext
from eval.embedder import TeiEmbedder
from eval.evidence_common import (
    EMBED_MODEL,
    RENDER_BUDGET_TOKENS,
    TrialUnit,
    covers_all,
    rank,
    rendered_token_counts,
)
from eval.qasper_common import Case, Paper, legacy_units_for_paper, load_papers, score_case

TRIAL_VERSION = "zenith-qasper-packets-fixed-legacy-206-v4"
BOOTSTRAP_SEED = 1729
BOOTSTRAP_SAMPLES = 2000
EXCEPTION = re.compile(r"\b(?:except|unless|however|notwithstanding)\b", re.I)


def make_hits(
    paper: Paper, units: list[TrialUnit], *, source_version: str = "qasper-v0.3"
) -> tuple[list[Hit], dict[UUID, TrialUnit]]:
    document = uuid5(NAMESPACE_URL, f"{source_version}:{paper.id}")
    source_hash = hashlib.sha256(paper.text.encode()).hexdigest()
    hits: list[Hit] = []
    by_id: dict[UUID, TrialUnit] = {}
    for unit in units:
        identity = uuid5(NAMESPACE_URL, f"{source_version}:{paper.id}:{unit.id}")
        hits.append(
            Hit(
                chunk_id=identity,
                document_id=document,
                filename=f"{paper.id}.txt",
                media_type="text/plain",
                page_num=None,
                char_start=unit.start,
                char_end=unit.end,
                text=unit.text,
                bboxes=[],
                lexical_rank=None,
                dense_rank=None,
                score=0.0,
                source_sha256=source_hash,
            )
        )
        by_id[identity] = unit
    return hits, by_id


def _score_selected(
    case: Case, selected: list[TrialUnit], counts: dict[str, int]
) -> dict[str, object]:
    complete = any(covers_all(selected, option) for option in case.evidence_options)
    recall = max(
        sum(covers_all(selected, (gold,)) for gold in option) / len(option)
        for option in case.evidence_options
    )
    top1 = bool(selected) and any(
        any(selected[0].start < end and selected[0].end > start for start, end in option)
        for option in case.evidence_options
    )
    return {
        "complete_evidence": complete,
        "evidence_recall": recall,
        "top1_intersects_evidence": top1,
        "rendered_tokens": sum(counts[item.id] for item in selected),
        "selected_units": len(selected),
    }


async def packet_routes(
    paper: Paper,
    case: Case,
    ordered: list[TrialUnit],
    all_hits: list[Hit],
    by_id: dict[UUID, TrialUnit],
    counts: dict[str, int],
) -> tuple[dict[str, object], dict[str, object]]:
    by_unit = {unit.id: hit for unit, hit in zip(by_id.values(), all_hits, strict=True)}
    positions = {hit.chunk_id: index for index, hit in enumerate(all_hits)}

    async def neighbor(context: TenantContext, hit: Hit, *, before: bool) -> Hit | None:
        index = positions[hit.chunk_id] + (-1 if before else 1)
        return all_hits[index] if 0 <= index < len(all_hits) else None

    async def referenced(context: TenantContext, hit: Hit, reference: str) -> Hit | None:
        heading = packet.section_heading_pattern(reference)
        if heading.search(hit.text):
            return hit
        matches = [
            item for item in all_hits if item.chunk_id != hit.chunk_id and heading.search(item.text)
        ]
        return matches[0] if len(matches) == 1 else None

    async def counter(
        context: TenantContext, hit: Hit, excluded: set[UUID], cap: int
    ) -> tuple[Hit, ...]:
        return tuple(
            item
            for item in all_hits
            if item.chunk_id not in excluded and EXCEPTION.search(item.text)
        )[:cap]

    def token_cost(hits: list[Hit], reasons: list[str]) -> int:
        return sum(counts[by_id[item.chunk_id].id] for item in hits)

    candidate_hits = [by_unit[unit.id] for unit in ordered]
    scope = TenantContext.for_tenant(candidate_hits[0].document_id)
    with (
        patch.object(packet, "_neighbor", neighbor),
        patch.object(packet, "_referenced", referenced),
        patch.object(packet, "_counter_candidates", counter),
    ):
        plain = await packet.build_packet(
            scope,
            case.question,
            candidate_hits,
            token_budget=RENDER_BUDGET_TOKENS,
            render_cost=token_cost,
            cost_unit="bge_m3_source_tokens",
        )
        countered = await packet.build_packet(
            scope,
            case.question,
            candidate_hits,
            token_budget=RENDER_BUDGET_TOKENS,
            render_cost=token_cost,
            cost_unit="bge_m3_source_tokens",
            counterevidence=True,
            max_counterevidence=2,
        )

    def outcome(built: packet.EvidencePacket) -> dict[str, object]:
        selected = [by_id[item.chunk_id] for item in built.hits]
        return {
            **_score_selected(case, selected, counts),
            "unresolved": list(built.unresolved),
            "counterevidence_candidates": len(built.counterevidence_ids),
            "counterevidence_status": built.counterevidence_status,
        }

    return outcome(plain), outcome(countered)


async def run(dev_json: Path, *, embed_url: str, rerank_url: str) -> dict[str, object]:
    papers = load_papers(dev_json)
    return await evaluate_papers(
        papers,
        dataset_sha256=hashlib.sha256(dev_json.read_bytes()).hexdigest(),
        trial_version=TRIAL_VERSION,
        source_version="qasper-v0.3",
        label_provenance="QASPER practitioner source paragraphs with exact unique mapping",
        embed_url=embed_url,
        rerank_url=rerank_url,
    )


async def evaluate_papers(
    papers: tuple[Paper, ...],
    *,
    dataset_sha256: str,
    trial_version: str,
    source_version: str,
    label_provenance: str,
    embed_url: str,
    rerank_url: str,
) -> dict[str, object]:
    embedder = TeiEmbedder(embed_url)
    reranker = TeiReranker(url=rerank_url, profile=PROFILES["gpu"])
    rows: list[dict[str, object]] = []
    for paper_number, paper in enumerate(papers, 1):
        units = legacy_units_for_paper(paper)
        hits, by_id = make_hits(paper, units, source_version=source_version)
        vectors = embedder.encode([unit.text for unit in units], batch=4)
        queries = embedder.encode([case.question for case in paper.cases], batch=4)
        counts = rendered_token_counts(units, embed_url)
        for case, query_vector in zip(paper.cases, queries, strict=True):
            dense_order = rank(units, vectors, query_vector)[:8]
            tei_scores = await reranker.rank(case.question, [unit.text for unit in dense_order])
            tei_order = [dense_order[item.index] for item in tei_scores]
            dense_packet, dense_counter = await packet_routes(
                paper, case, dense_order, hits, by_id, counts
            )
            tei_packet, tei_counter = await packet_routes(
                paper, case, tei_order, hits, by_id, counts
            )
            rows.append(
                {
                    "paper_id": paper.id,
                    "question_id": case.id,
                    "label_choice": case.label_choice,
                    "routes": {
                        "dense_topk": score_case(case, dense_order, counts),
                        "tei_topk": score_case(case, tei_order, counts),
                        "dense_packet": dense_packet,
                        "tei_packet": tei_packet,
                        "dense_packet_counter": dense_counter,
                        "tei_packet_counter": tei_counter,
                    },
                }
            )
        print(f"  evaluated public packet paper {paper_number}/{len(papers)}", flush=True)
    routes = (
        "dense_topk",
        "tei_topk",
        "dense_packet",
        "tei_packet",
        "dense_packet_counter",
        "tei_packet_counter",
    )
    summary: dict[str, dict[str, float | int]] = {}
    for name in routes:
        scores = [
            cast(dict[str, object], cast(dict[str, object], row["routes"])[name]) for row in rows
        ]
        summary[name] = {
            "cases": len(scores),
            "complete_evidence_rate": statistics.mean(
                float(cast(bool, score["complete_evidence"])) for score in scores
            ),
            "mean_evidence_recall": statistics.mean(
                cast(float, score["evidence_recall"]) for score in scores
            ),
            "mean_rendered_tokens": statistics.mean(
                cast(int, score["rendered_tokens"]) for score in scores
            ),
            "unresolved_cases": sum(bool(score.get("unresolved")) for score in scores),
            "counterevidence_candidates": sum(
                cast(int, score.get("counterevidence_candidates", 0)) for score in scores
            ),
        }
    rng = random.Random(BOOTSTRAP_SEED)
    intervals: dict[str, object] = {}
    for route, baseline in (
        ("dense_packet", "dense_topk"),
        ("tei_packet", "tei_topk"),
        ("dense_packet_counter", "dense_packet"),
        ("tei_packet_counter", "tei_packet"),
    ):
        by_paper: dict[str, list[float]] = {}
        for row in rows:
            scores = cast(dict[str, dict[str, object]], row["routes"])
            difference = float(cast(bool, scores[route]["complete_evidence"])) - float(
                cast(bool, scores[baseline]["complete_evidence"])
            )
            by_paper.setdefault(cast(str, row["paper_id"]), []).append(difference)
        differences = [statistics.mean(values) for values in by_paper.values()]
        replicates = sorted(
            statistics.mean(rng.choice(differences) for _ in differences)
            for _ in range(BOOTSTRAP_SAMPLES)
        )
        intervals[route] = {
            "vs": baseline,
            "macro_paper_difference": statistics.mean(differences),
            "percentile_95_interval": [replicates[49], replicates[1949]],
            "paper_clusters": len(differences),
        }
    return {
        "trial_version": trial_version,
        "dataset_sha256": dataset_sha256,
        "source_ids": [paper.id for paper in papers],
        "eligible_questions": len(rows),
        "candidate_policy": (
            "same dense top-8 current chunks; local TEI reorders the same shortlist"
        ),
        "embedding_model": EMBED_MODEL,
        "tei_reported_model": await reranker.model_identity(),
        "render_budget_source_tokens": RENDER_BUDGET_TOKENS,
        "tokenizer_endpoint": embed_url,
        "reranker_endpoint": rerank_url,
        "label_provenance": label_provenance,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "bootstrap_samples": BOOTSTRAP_SAMPLES,
        "summary": summary,
        "paired_paper_bootstrap": intervals,
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-json", type=Path, required=True)
    parser.add_argument("--embed-url", default="http://127.0.0.1:18081")
    parser.add_argument("--rerank-url", default="http://127.0.0.1:18083")
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    report = asyncio.run(
        run(options.dev_json, embed_url=options.embed_url, rerank_url=options.rerank_url)
    )
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {options.output}")


if __name__ == "__main__":
    main()
