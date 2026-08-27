# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""Scoring the running installation with one profile field changed.

Extracted when a second sweep needed it. `ef_search.py` asked what `hnsw_ef_search` was
worth; `rerank_depth.py` asks the same question of `rerank_candidates`, and the half that
matters — *does the change reach the page a reader sees* — was identical in both. Copying it
would have meant two credit rules drifting apart, and a credit rule that differs between two
reports makes the reports incomparable, which is the one thing they exist to be.

The rule is `eval/live.py`'s, deliberately: a passage counts when its `(document, page)` is
one the question records. Sharing it is what lets a number here be read beside
`live-recall.json`.

**Read-only.** The queries this runs are searches; nothing writes.
"""

import dataclasses
import statistics
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.hardware import Profile
from app.core.hardware import active as active_profile
from app.features.auth.service import AccessProfile
from app.features.retrieval.service import SearchService
from app.features.tenancy.context import TenantContext
from eval.live import FILENAMES
from eval.questions import Question, load_questions

#: What the interface shows. Scoring deeper than the product displays would measure a page
#: nobody reads.
PAGE = 8


@dataclass(frozen=True, slots=True)
class Installation:
    """Whatever corpus is loaded, and the identity needed to search it."""

    tenant: UUID
    labels: tuple[UUID, ...]
    user: UUID
    space: Any
    questions: list[Question]

    @property
    def profile(self) -> AccessProfile:
        return AccessProfile(
            user_id=self.user,
            context=TenantContext(tenant_id=self.tenant, label_ids=self.labels, user_id=self.user),
            permissions=frozenset(),
        )


async def installation() -> Installation:
    """Read the tenant holding the corpus, and the questions its documents can answer.

    The tenant is chosen by chunk count rather than by name: an installation may hold
    several archives, and the measured one is the one with the passages in it.

    Questions whose document was never uploaded are excluded, not counted as misses — same
    rule as `live.py`. Scoring them would measure which files happen to be present rather
    than how well retrieval ranks, and a number that moves when somebody deletes a PDF is
    not a quality metric.
    """
    engine = create_async_engine(settings.database_owner_url)
    async with engine.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        space = (
            await conn.execute(
                text(
                    "SELECT embedding_model AS model, embedding_version AS version, "
                    "count(*) AS n FROM chunk_embeddings GROUP BY 1, 2 ORDER BY n DESC LIMIT 1"
                )
            )
        ).one()
        tenant = (
            await conn.execute(
                text(
                    "SELECT t.id FROM tenants t JOIN chunks c ON c.tenant_id = t.id "
                    "GROUP BY t.id ORDER BY count(*) DESC LIMIT 1"
                )
            )
        ).scalar_one()
        labels = tuple((await conn.execute(text("SELECT id FROM access_labels"))).scalars())
        user = (
            await conn.execute(
                text("SELECT id FROM users WHERE tenant_id = :t LIMIT 1"), {"t": tenant}
            )
        ).scalar_one()
        rows = await conn.execute(
            text("SELECT filename FROM documents WHERE tenant_id = :t AND status = 'ready'"),
            {"t": tenant},
        )
        ready = set(rows.scalars())
        await conn.rollback()
    await engine.dispose()

    questions = [
        question
        for question in load_questions()
        if question.sources and FILENAMES.get(question.sources[0].document) in ready
    ]
    return Installation(tenant=tenant, labels=labels, user=user, space=space, questions=questions)


async def score(where: Installation, **overrides: object) -> dict[str, object]:
    """Run every question through the whole path with the profile fields overridden.

    `overrides` names fields of `hardware.Profile`. Passing none scores the profile the
    installation is configured with, which is the baseline every sweep needs.
    """
    hardware: Profile = dataclasses.replace(active_profile(), **overrides)  # type: ignore[arg-type]
    service = SearchService(where.profile, hardware=hardware)

    ranks: list[tuple[str, bool, int | None]] = []
    times: list[float] = []
    degraded = 0

    for question in where.questions:
        wanted = {
            (FILENAMES[source.document], page)
            for source in question.sources
            for page in source.pages
        }
        started = time.perf_counter()
        result = await service.search(question.question, limit=PAGE)
        times.append((time.perf_counter() - started) * 1000)
        degraded += 1 if result.degraded else 0
        rank = next(
            (
                position + 1
                for position, hit in enumerate(result.hits)
                if (hit.filename, hit.page_num) in wanted
            ),
            None,
        )
        ranks.append((question.id, question.counts_towards_headline, rank))

    headline = [rank for _, counts, rank in ranks if counts]
    found = [rank for _, _, rank in ranks if rank]
    return {
        "headline_recall_at_8": round(sum(r is not None for r in headline) / len(headline), 4),
        "headline_scored": len(headline),
        "recall_at_8_all": round(sum(r is not None for _, _, r in ranks) / len(ranks), 4),
        "recall_at_1_all": round(sum(r == 1 for _, _, r in ranks) / len(ranks), 4),
        "mean_rank": round(statistics.mean(found), 3) if found else None,
        "median_ms": round(statistics.median(times)),
        "p95_ms": round(sorted(times)[int(len(times) * 0.95) - 1]),
        "degraded": degraded,
        "missed": [qid for qid, _, rank in ranks if rank is None],
    }
