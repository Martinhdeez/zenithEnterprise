# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""Latency on the machine a customer would actually buy.

Every number this project has quoted was measured on a MacBook, by one process, one request
at a time. Three things have therefore never been known:

1. **Is `rerank_candidates = 50` affordable on the `cpu` profile?** It has been an explicit
   assumption since F7 and was flagged twice as the most likely number in the profile table
   to be wrong.
2. **What happens with more than one user?** `api_pool_size = 10` and
   `statement_timeout_ms = 10_000` have never met a second request.
3. **Can a query be answered while ingestion runs?** F5 measured ingestion holding TEI for
   13.4 minutes per 100 dense pages, and F6 designed a 5-second query timeout around that
   number. The interaction itself has never been run.

## The corpus here is synthetic, and that is correct

Chunks are generated text with random unit vectors rather than real documents. This measures
**speed, not quality** — HNSW search cost depends on the index's size and dimensionality,
not on whether the vectors mean anything, and `ts_rank_cd` cost depends on the text's length
and term distribution rather than its sense.

Quality is already measured, against real documents, by `eval.production` and
`eval.grounding`. Repeating it here would cost forty minutes of parsing to answer a question
that has an answer. Using synthetic data lets the corpus be sized to match production in
seconds.

What it cannot tell us is recall. Nothing here reports recall, for that reason.
"""

import argparse
import asyncio
import json
import random
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text

REPORT = Path(__file__).resolve().parent / "vps-benchmark.json"

# Matched to the local corpus (19,533 chunks over 39 documents) so the index this measures
# is the size of the one that produced every quality number so far.
CHUNKS = 19_500
DOCUMENTS = 39

# The profile whose TEI settings this hardware can actually run. See `measure_concurrency`.
BASE_PROFILE = "low-spec"

VOCABULARY = [
    "regulation",
    "controller",
    "processor",
    "personal",
    "data",
    "subject",
    "supervisory",
    "authority",
    "consent",
    "legitimate",
    "interest",
    "processing",
    "purpose",
    "retention",
    "erasure",
    "portability",
    "rectification",
    "breach",
    "notification",
    "impact",
    "assessment",
    "transfer",
    "adequacy",
    "safeguards",
    "binding",
    "corporate",
    "rules",
    "employer",
    "withholding",
    "wages",
    "medicare",
    "social",
    "security",
    "taxable",
    "compensation",
    "deduction",
    "credit",
    "allowance",
    "exemption",
    "filing",
    "status",
    "dependent",
    "return",
    "payroll",
    "deposit",
    "schedule",
    "penalty",
    "transformer",
    "attention",
    "encoder",
    "decoder",
    "embedding",
    "token",
    "sequence",
    "layer",
    "normalisation",
    "residual",
    "retrieval",
    "passage",
    "index",
    "generation",
    "pretraining",
    "fine-tuning",
    "benchmark",
    "evaluation",
    "corpus",
    "platform",
    "provider",
    "intermediary",
    "hosting",
    "notice",
    "flagger",
    "moderation",
    "transparency",
    "systemic",
    "risk",
]


@dataclass
class Sample:
    phase: str
    concurrency: int
    latency_ms: float
    rerank_candidates: int = 0
    error: str | None = None
    # Counted separately from `error`, and the reason this field exists at all: the first
    # run of this benchmark reported "failures 0" while *every* rerank request was being
    # rejected by TEI. The search degrades rather than raising — by design, so a customer
    # keeps getting answers — which means a benchmark that only counts exceptions measures
    # a reranker that never ran and calls it fast.
    degraded: bool = False
    reason: str | None = None


@dataclass
class Report:
    cores: int = 0
    samples: list[Sample] = field(default_factory=list[Sample])


def sentence(words: int = 180) -> str:
    return " ".join(random.choice(VOCABULARY) for _ in range(words))


def unit_vector(dimension: int) -> list[float]:
    raw = [random.gauss(0, 1) for _ in range(dimension)]
    norm = sum(value * value for value in raw) ** 0.5
    return [value / norm for value in raw]


async def provision() -> tuple[UUID, UUID, UUID]:
    from app.core.database import owner_session
    from app.features.tenancy.service import TenantService

    tenant = await TenantService().create(f"bench {uuid4()}")
    async with owner_session() as session:
        label_id = await session.scalar(
            text("SELECT id FROM access_labels WHERE tenant_id = :t AND is_default"),
            {"t": tenant.id},
        )
        user_id = await session.scalar(
            text("SELECT id FROM users WHERE tenant_id = :t LIMIT 1"), {"t": tenant.id}
        )
    return tenant.id, UUID(str(label_id)), UUID(str(user_id or uuid4()))


async def seed(tenant_id: UUID, label_id: UUID, chunks: int) -> None:
    """Fill the index to production size.

    Written through `executemany` in batches rather than row by row: 19,500 round trips to
    Postgres would dominate the setup time and teach us nothing.
    """
    from app.core.database import owner_session
    from app.features.embeddings.client import DIMENSION, MODEL, VERSION

    async with owner_session() as session:
        await session.execute(
            text(
                "INSERT INTO embedding_spaces (model, version, dimension, status) "
                "VALUES (:m, :v, :d, 'active') ON CONFLICT DO NOTHING"
            ),
            {"m": MODEL, "v": VERSION, "d": DIMENSION},
        )
        document_ids: list[UUID] = []
        for index in range(DOCUMENTS):
            document_id = await session.scalar(
                text(
                    "INSERT INTO documents (tenant_id, filename, sha256, size_bytes, status, "
                    "page_count) VALUES (:t, :name, :sha, 1000, 'ready', 100) RETURNING id"
                ),
                {
                    "t": tenant_id,
                    "name": f"bench-{index}.pdf",
                    "sha": uuid4().hex + uuid4().hex[:32],
                },
            )
            await session.execute(
                text("INSERT INTO document_labels (document_id, label_id) VALUES (:d, :l)"),
                {"d": document_id, "l": label_id},
            )
            document_ids.append(UUID(str(document_id)))

        written = 0
        while written < chunks:
            batch = min(500, chunks - written)
            rows = [
                {
                    "d": str(document_ids[(written + offset) % DOCUMENTS]),
                    "t": str(tenant_id),
                    "p": (written + offset) % 100 + 1,
                    "body": sentence(),
                    "embedding": str(unit_vector(DIMENSION)),
                    "m": MODEL,
                    "v": VERSION,
                }
                for offset in range(batch)
            ]
            await session.execute(
                text(
                    "WITH inserted AS ("
                    "  INSERT INTO chunks (document_id, tenant_id, page_num, char_start, "
                    "  char_end, text, bboxes) "
                    "  VALUES (CAST(:d AS uuid), CAST(:t AS uuid), :p, 0, 1000, :body, '[]') "
                    "  RETURNING id, tenant_id"
                    ") "
                    "INSERT INTO chunk_embeddings (chunk_id, tenant_id, embedding_model, "
                    "embedding_version, embedding) "
                    "SELECT id, tenant_id, :m, :v, CAST(:embedding AS vector) FROM inserted"
                ),
                rows,
            )
            written += batch
            if written % 2500 == 0:
                print(f"  seeded {written}/{chunks}", flush=True)


def profile_for(tenant_id: UUID, label_id: UUID, user_id: UUID) -> object:
    from app.features.auth.permissions import CATALOGUE
    from app.features.auth.service import AccessProfile
    from app.features.tenancy.context import TenantContext

    return AccessProfile(
        user_id=user_id,
        context=TenantContext.for_tenant(tenant_id, [label_id]),
        permissions=frozenset(CATALOGUE),
    )


QUESTIONS = [
    "What obligations does a controller have when a personal data breach occurs?",
    "Which filing status applies to a dependent with wage income?",
    "How does the attention mechanism handle long sequences?",
    "What transparency duties fall on a hosting provider?",
    "Which retention period applies to payroll records?",
]


async def one_query(service: object, question: str, candidates: int) -> Sample:
    started = time.perf_counter()
    error, degraded, reason = None, False, None
    try:
        result = await service.search(question, limit=8)  # type: ignore[attr-defined]
        degraded, reason = result.degraded, result.reason
    except Exception as exc:  # noqa: BLE001 - a failed request is a data point
        error = f"{type(exc).__name__}: {exc}"
    return Sample(
        phase="query",
        concurrency=0,
        latency_ms=(time.perf_counter() - started) * 1000,
        rerank_candidates=candidates,
        error=error,
        degraded=degraded,
        reason=reason,
    )


async def measure_concurrency(
    account: tuple[UUID, UUID, UUID], levels: list[int], candidates: int, rounds: int
) -> list[Sample]:
    """The question `api_pool_size = 10` has never been asked.

    Each level runs `rounds` waves of `level` simultaneous searches. Latency is reported per
    request, so a level that simply queues shows up as a rising median rather than as an
    error — which is the failure mode a customer would actually experience.
    """
    from dataclasses import replace

    from app.core.hardware import PROFILES
    from app.features.embeddings.client import TeiClient
    from app.features.retrieval.reranker import TeiReranker
    from app.features.retrieval.service import SearchService

    # `replace` rather than unpacking `asdict`: the profile is a frozen dataclass, and
    # rebuilding it from a dict would silently accept a typo as a new field.
    #
    # Based on `low-spec` rather than `cpu`, and that is a measurement, not a preference:
    # starting TEI with the `cpu` profile's `--max-batch-tokens 8192` was OOM-killed on
    # this machine at 4.3 GB RSS during warm-up, before serving one request. The client's
    # batch sizing must match what the server can actually be started with.
    profile = replace(PROFILES[BASE_PROFILE], rerank_candidates=candidates, reranker=candidates > 0)
    samples: list[Sample] = []

    for level in levels:
        for wave in range(rounds):
            services = [
                SearchService(
                    profile_for(*account),  # type: ignore[arg-type]
                    embedder=TeiClient(profile=profile),
                    hardware=profile,
                    reranker=TeiReranker(profile=profile) if candidates else None,
                )
                for _ in range(level)
            ]
            results = await asyncio.gather(
                *(
                    one_query(service, QUESTIONS[(wave + index) % len(QUESTIONS)], candidates)
                    for index, service in enumerate(services)
                )
            )
            for sample in results:
                sample.concurrency = level
                samples.append(sample)
        median = statistics.median(
            s.latency_ms for s in samples if s.concurrency == level and not s.error
        )
        recent = [s for s in samples if s.concurrency == level]
        failures = sum(1 for s in recent if s.error)
        degraded = sum(1 for s in recent if s.degraded)
        note = ""
        if degraded:
            # Loud, because a degraded search still returns results and still looks fast.
            example = next(s.reason for s in recent if s.degraded)
            note = f"  DEGRADED {degraded}/{len(recent)} — {example}"
        print(
            f"  concurrency {level:>2}  candidates {candidates:>3}  "
            f"median {median:>8.0f} ms  failures {failures}{note}",
            flush=True,
        )
    return samples


async def sweep_rerank_candidates(
    account: tuple[UUID, UUID, UUID], counts: list[int], rounds: int
) -> list[Sample]:
    """The number F7 guessed, measured.

    `rerank_candidates` decides how many passages the cross-encoder reads on every
    interactive request. On four CPU cores that is the dominant cost of a query, and 50 was
    chosen by reasoning about a laptop.
    """
    samples: list[Sample] = []
    for count in counts:
        samples.extend(await measure_concurrency(account, [1], count, rounds))
    return samples


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks", type=int, default=CHUNKS)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--skip-seed", action="store_true")
    arguments = parser.parse_args()

    import os

    from app.core.database import owner_session

    report = Report(cores=os.cpu_count() or 0)

    if arguments.skip_seed:
        async with owner_session() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT d.tenant_id FROM documents d "
                        "WHERE EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = d.id) LIMIT 1"
                    )
                )
            ).first()
            assert row is not None, "nothing seeded; run without --skip-seed"
            label_id = await session.scalar(
                text("SELECT id FROM access_labels WHERE tenant_id = :t AND is_default"),
                {"t": row.tenant_id},
            )
            user_id = await session.scalar(
                text("SELECT id FROM users WHERE tenant_id = :t LIMIT 1"), {"t": row.tenant_id}
            )
        # A benchmark tenant has no users — nothing logs in. The id only has to be a valid
        # UUID for the access profile; RLS scopes on the tenant and the label.
        account = (row.tenant_id, UUID(str(label_id)), UUID(str(user_id)) if user_id else uuid4())
    else:
        account = await provision()
        print(f"seeding {arguments.chunks} chunks", flush=True)
        started = time.perf_counter()
        await seed(account[0], account[1], arguments.chunks)
        print(f"seeded in {time.perf_counter() - started:.0f}s", flush=True)

    print("\n--- rerank_candidates sweep (concurrency 1)", flush=True)
    report.samples.extend(
        await sweep_rerank_candidates(account, [0, 10, 25, 50, 100], arguments.rounds)
    )

    print("\n--- concurrency, reranking 50 candidates", flush=True)
    report.samples.extend(await measure_concurrency(account, [2, 5, 10], 50, arguments.rounds))

    print("\n--- concurrency, no reranker (low-spec behaviour)", flush=True)
    report.samples.extend(await measure_concurrency(account, [2, 5, 10], 0, arguments.rounds))

    REPORT.write_text(
        json.dumps(
            {"cores": report.cores, "samples": [asdict(s) for s in report.samples]}, indent=2
        )
    )
    print(f"\nwrote {REPORT}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
