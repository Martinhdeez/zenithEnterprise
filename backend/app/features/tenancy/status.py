"""What state is this tenant in — the question every client asks before anything else.

Today a UI opening the app has to call three endpoints and guess: is there anything to
search, is ingestion still running, which components does this installation even have. One
call answers it.

Counts come from the caller's own session, so the policies scope them and no bypass is
needed. A number here is a number about *this* customer by construction rather than by a
`WHERE` clause someone has to remember.

**And they are label-scoped, not merely tenant-scoped — that is a security property.**
mvp.md 3.1 requires that a user who cannot reach a document also cannot *deduce that it
exists*. Reporting "41 documents" to someone who can open two would leak the size of a
compartment they are locked out of, on the first screen, to every user, on every page load.
Running inside `tenant_session` with the caller's real context is what makes that
impossible rather than merely unlikely.
"""

from dataclasses import dataclass, field

from sqlalchemy import text

from app.core.database import tenant_session
from app.core.hardware import Profile
from app.core.hardware import active as active_profile
from app.features.tenancy.context import TenantContext


@dataclass(frozen=True, slots=True)
class Components:
    """What this installation is configured to have — **not** what answered a ping.

    Deliberately configuration rather than liveness. Probing three services would turn one
    page load into three network round trips, make this the slowest route in the product,
    and still be stale by the time it rendered. Liveness is what `degraded` on a real
    answer reports: it is measured on the request that actually needed the component.
    """

    embeddings: bool
    reranker: bool
    generation: bool


@dataclass(frozen=True, slots=True)
class TenantStatus:
    documents: dict[str, int] = field(default_factory=dict[str, int])
    chunks: int = 0
    hardware: str = ""
    components: Components = field(
        default_factory=lambda: Components(embeddings=True, reranker=False, generation=False)
    )
    #: Computed, never stored: `chunks > 0` is the only thing that decides whether search
    #: can return anything. A UI left to derive it will derive it differently in three
    #: places, and two of them will be wrong about an empty corpus.
    searchable: bool = False


async def status(context: TenantContext, hardware: Profile | None = None) -> TenantStatus:
    profile = hardware or active_profile()

    async with tenant_session(context) as session:
        rows = await session.execute(
            text("SELECT status, count(*) AS total FROM documents GROUP BY status")
        )
        documents = {row.status: int(row.total) for row in rows}
        chunks = await session.scalar(text("SELECT count(*) FROM chunks")) or 0

    return TenantStatus(
        documents=documents,
        chunks=int(chunks),
        hardware=profile.name,
        components=Components(
            # The embedder is not optional: without it there is no dense half and no
            # ingestion at all, so an installation always has one configured.
            embeddings=True,
            reranker=profile.reranker,
            generation=_generation_configured(),
        ),
        searchable=int(chunks) > 0,
    )


def _generation_configured() -> bool:
    """Whether a question could be answered at all, as opposed to merely searched.

    Reports the installation default only. A tenant's own `llm_config` row would need a
    query, and this is the route that must stay cheap — a client seeing `false` here and a
    working answer is a better failure than a status page that costs a join.
    """
    from app.core.config import settings

    return bool(settings.llm_endpoint_url and settings.llm_model)
