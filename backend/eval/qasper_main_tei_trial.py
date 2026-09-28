"""All-eligible QASPER baseline for Zenith's existing local TEI reranker.

This is the current chunker and actual local TEI ranker on a frozen dense
candidate set. It uses no Jev calls and does not substitute for full SQL hybrid
retrieval or answer generation.
"""

import argparse
import asyncio
import hashlib
import json
import random
import statistics
import time
from pathlib import Path
from typing import cast

from app.core.hardware import PROFILES
from app.features.retrieval.reranker import TeiReranker
from eval.embedder import TeiEmbedder
from eval.lossless_trial import EMBED_MODEL, rank, rendered_token_counts
from eval.qasper_trial import DEV_SHA256, Paper, load_papers, score_case, units_for_paper

TRIAL_VERSION = "zenith-qasper-dev-64-current-tei-v1"
BOOTSTRAP_SEED = 1729
BOOTSTRAP_SAMPLES = 2000


def metric(value: object, name: str) -> float:
    if not isinstance(value, dict):
        raise ValueError("invalid benchmark score")
    score = cast("dict[str, object]", value).get(name)
    if not isinstance(score, (bool, int, float)):
        raise ValueError(f"missing benchmark metric: {name}")
    return float(score)


async def run(dev_json: Path, *, embed_url: str, rerank_url: str) -> dict[str, object]:
    papers = load_papers(dev_json)
    embedder = TeiEmbedder(embed_url)
    reranker = TeiReranker(url=rerank_url, profile=PROFILES["gpu"])
    rows: list[dict[str, object]] = []
    for paper_number, paper in enumerate(papers, 1):
        await evaluate_paper(paper, embedder, reranker, embed_url, rows)
        print(f"  evaluated public paper {paper_number}/{len(papers)}", flush=True)
    if not rows:
        raise ValueError("frozen benchmark has no mappable questions")
    summary: dict[str, dict[str, float | int]] = {}
    for route in ("dense", "main_tei"):
        scores = [row[route] for row in rows]
        summary[route] = {
            "cases": len(scores),
            "complete_evidence_rate": statistics.mean(
                metric(score, "complete_evidence") for score in scores
            ),
            "mean_evidence_recall": statistics.mean(
                metric(score, "evidence_recall") for score in scores
            ),
            "top1_intersects_evidence_rate": statistics.mean(
                metric(score, "top1_intersects_evidence") for score in scores
            ),
        }
    by_paper: dict[str, list[float]] = {}
    for row in rows:
        paper_id = str(row["paper_id"])
        dense = row["dense"]
        main = row["main_tei"]
        by_paper.setdefault(paper_id, []).append(
            metric(main, "complete_evidence") - metric(dense, "complete_evidence")
        )
    differences = [statistics.mean(values) for values in by_paper.values()]
    rng = random.Random(BOOTSTRAP_SEED)
    replicates = sorted(
        statistics.mean(rng.choice(differences) for _ in differences)
        for _ in range(BOOTSTRAP_SAMPLES)
    )
    return {
        "trial_version": TRIAL_VERSION,
        "dataset_sha256": hashlib.sha256(dev_json.read_bytes()).hexdigest(),
        "expected_dataset_sha256": DEV_SHA256,
        "selection": "all exact-evidence questions in 64 hash-selected QASPER dev papers",
        "source_ids": [paper.id for paper in papers],
        "eligible_questions": len(rows),
        "all_questions_in_selected_papers": sum(paper.questions for paper in papers),
        "candidate_policy": "same dense top-8 current chunks before TEI rerank",
        "embedding_model": EMBED_MODEL,
        "tei_reported_model": await reranker.model_identity(),
        "embed_url": embed_url,
        "rerank_url": rerank_url,
        "render_budget_tokens": 1100,
        "summary": summary,
        "paired_paper_bootstrap": {
            "seed": BOOTSTRAP_SEED,
            "samples": BOOTSTRAP_SAMPLES,
            "macro_paper_difference": statistics.mean(differences),
            "percentile_95_interval": [replicates[49], replicates[1949]],
            "paper_clusters": len(differences),
        },
        "rows": rows,
    }


async def evaluate_paper(
    paper: Paper,
    embedder: TeiEmbedder,
    reranker: TeiReranker,
    embed_url: str,
    rows: list[dict[str, object]],
) -> None:
    legacy, _ = units_for_paper(paper)
    vectors = embedder.encode([unit.text for unit in legacy], batch=4)
    queries = embedder.encode([case.question for case in paper.cases], batch=4)
    token_counts = rendered_token_counts(legacy, embed_url)
    for case, query_vector in zip(paper.cases, queries, strict=True):
        top = rank(legacy, vectors, query_vector)[:8]
        started = time.perf_counter()
        reranked = await reranker.rank(case.question, [unit.text for unit in top])
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        if len(reranked) != len(top):
            raise ValueError("TEI did not rank the fixed candidate set")
        rows.append(
            {
                "paper_id": paper.id,
                "question_id": case.id,
                "dense": score_case(case, top, token_counts),
                "main_tei": {
                    **score_case(case, [top[item.index] for item in reranked], token_counts),
                    "elapsed_ms": elapsed_ms,
                },
            }
        )


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
