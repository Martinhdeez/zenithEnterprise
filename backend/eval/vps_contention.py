# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

"""Can a customer ask a question while a colleague uploads a document?

F5 measured ingestion holding TEI for **13.4 minutes per 100 dense pages** on this exact
machine. F6 read that number and designed around it: `embed_query` gets a five-second
timeout, and when it expires the search returns the lexical half alone and says `degraded`.

That design has never been run. The interaction is the whole point — a product where the
first upload makes search unusable for twelve minutes is not a product, and a `degraded`
flag that fires on every query during ingestion is a different failure from one that never
fires at all.

**Ingestion is simulated by its effect on TEI, not by parsing PDFs.** What ingestion does to
an interactive query is occupy the embedding service with batches; whether the text came
from a PDF is irrelevant to the contention. Simulating it directly means the load is
steady and controllable instead of depending on which page of which document happens to be
parsing.
"""

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text

from eval.vps_bench import QUESTIONS, profile_for, sentence

REPORT = Path(__file__).resolve().parent / "vps-contention.json"


@dataclass
class Contended:
    concurrency: int
    ingesting: bool
    latency_ms: float
    degraded: bool
    reason: str | None = None


async def ingestion_load(stop: asyncio.Event) -> int:
    """What ingestion looks like from TEI's point of view: batches, back to back.

    Uses the profile's own client so the batch sizing is the shipped sizing — the same
    token budget that F5 arrived at after a 413.
    """
    from app.core.hardware import PROFILES
    from app.features.embeddings.client import TeiClient

    client = TeiClient(profile=PROFILES["low-spec"])
    batches = 0
    while not stop.is_set():
        try:
            await client.embed([sentence(120) for _ in range(4)])
            batches += 1
        except Exception:  # noqa: BLE001 - the load generator must not stop the measurement
            await asyncio.sleep(0.5)
    return batches


async def query_once(service: object, question: str, ingesting: bool, level: int) -> Contended:
    started = time.perf_counter()
    degraded, reason = False, None
    try:
        result = await service.search(question, limit=8)  # type: ignore[attr-defined]
        degraded, reason = result.degraded, result.reason
    except Exception as exc:  # noqa: BLE001
        degraded, reason = True, f"{type(exc).__name__}: {exc}"
    return Contended(
        concurrency=level,
        ingesting=ingesting,
        latency_ms=(time.perf_counter() - started) * 1000,
        degraded=degraded,
        reason=reason,
    )


async def measure(
    account: tuple[UUID, UUID, UUID], level: int, rounds: int, ingesting: bool
) -> list[Contended]:
    from app.core.hardware import PROFILES
    from app.features.embeddings.client import TeiClient
    from app.features.retrieval.service import SearchService

    profile = PROFILES["low-spec"]  # reranker off: the profile this hardware ships with
    samples: list[Contended] = []
    for wave in range(rounds):
        services = [
            SearchService(
                profile_for(*account),  # type: ignore[arg-type]
                embedder=TeiClient(profile=profile),
                hardware=profile,
            )
            for _ in range(level)
        ]
        samples.extend(
            await asyncio.gather(
                *(
                    query_once(service, QUESTIONS[(wave + i) % len(QUESTIONS)], ingesting, level)
                    for i, service in enumerate(services)
                )
            )
        )
    return samples


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=4)
    arguments = parser.parse_args()

    from app.core.database import owner_session

    async with owner_session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT d.tenant_id FROM documents d "
                    "WHERE EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = d.id) LIMIT 1"
                )
            )
        ).first()
        assert row is not None, "seed the corpus with eval.vps_bench first"
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

    samples: list[Contended] = []
    for level in (1, 5):
        quiet = await measure(account, level, arguments.rounds, ingesting=False)
        samples.extend(quiet)
        print(
            f"  idle       concurrency {level}: median "
            f"{statistics.median(s.latency_ms for s in quiet):.0f} ms, "
            f"degraded {sum(s.degraded for s in quiet)}/{len(quiet)}",
            flush=True,
        )

        stop = asyncio.Event()
        loader = asyncio.create_task(ingestion_load(stop))
        await asyncio.sleep(3)  # let the load actually reach TEI before measuring
        busy = await measure(account, level, arguments.rounds, ingesting=True)
        stop.set()
        batches = await loader
        samples.extend(busy)
        print(
            f"  ingesting  concurrency {level}: median "
            f"{statistics.median(s.latency_ms for s in busy):.0f} ms, "
            f"degraded {sum(s.degraded for s in busy)}/{len(busy)} "
            f"({batches} embedding batches sent)",
            flush=True,
        )

    REPORT.write_text(json.dumps([asdict(s) for s in samples], indent=2))
    print(f"\nwrote {REPORT}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
