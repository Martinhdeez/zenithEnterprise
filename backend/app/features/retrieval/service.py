"""Search, end to end, and the one place it is allowed to return half an answer.

Query embedding is synchronous and in-request. On `low-spec` that means the user waits for
TEI behind whatever ingestion is doing, and F5 measured ingestion holding TEI for **13.4
minutes per 100 dense pages**. A second TEI instance for queries would fix it and double the
memory footprint of a profile defined by not having any, so the MVP answer is different:
**give the embedding call a short timeout, and if it expires, return the lexical half alone
and say so.**

Half a search in two seconds beats a whole one in forty, but only if the caller can tell
which one they got. `degraded` is in the response for that reason — a silently worse answer
is the failure mode this whole project keeps refusing.
"""

import time
from dataclasses import dataclass, replace
from uuid import UUID

import structlog

from app.common.exceptions import PermissionDeniedError
from app.core.database import tenant_session
from app.core.hardware import Profile
from app.core.hardware import active as active_profile
from app.features.auth.permissions import CATALOGUE
from app.features.auth.service import AccessProfile
from app.features.embeddings.client import MODEL, VERSION, TeiClient
from app.features.retrieval.breaker import Breaker
from app.features.retrieval.identifiers import exact
from app.features.retrieval.reranker import TeiReranker
from app.features.retrieval.search import (
    CANDIDATES,
    Hit,
    candidates,
    dense,
    fuse,
    hydrate,
    lexical,
)
from app.features.tenancy.context import TenantContext

log = structlog.get_logger()

EXECUTE = "query.execute"
assert EXECUTE in CATALOGUE, "the permission this service is gated on must exist"

DEFAULT_LIMIT = 8
MAX_LIMIT = 50

# One breaker per process, shared by every `SearchService` instance, because the thing it
# describes — is the reranker answering? — is a property of the deployment rather than of a
# request. A per-instance breaker would reset on every query and never open at all, which
# is the bug this module would otherwise ship with.
RERANKER_BREAKER = Breaker()


@dataclass(frozen=True, slots=True)
class SearchResult:
    hits: list[Hit]
    degraded: bool
    reason: str | None
    took_ms: int


class SearchService:
    def __init__(
        self,
        profile: AccessProfile,
        embedder: TeiClient | None = None,
        hardware: Profile | None = None,
        reranker: TeiReranker | None = None,
        breaker: Breaker | None = None,
    ) -> None:
        self.profile = profile
        # Injectable so a test can drive the states without waiting a minute of real time.
        self.breaker = breaker or RERANKER_BREAKER
        self.context = profile.context
        self.hardware = hardware or active_profile()
        self.embedder = embedder or TeiClient(profile=self.hardware)
        # `None` where the profile disables it. That is a configured product rather than a
        # failure, and the two must not report the same way — see `search`.
        self.reranker = (
            reranker
            if reranker is not None
            else (TeiReranker(profile=self.hardware) if self.hardware.reranker else None)
        )

    async def search(
        self, question: str, limit: int = DEFAULT_LIMIT, labels: list[UUID] | None = None
    ) -> SearchResult:
        started = time.perf_counter()
        limit = max(1, min(limit, MAX_LIMIT))
        context = self._narrowed(labels)

        embedding, degraded_reason = await self._embed(question)

        async with tenant_session(context) as session:
            lexical_scored = await lexical(session, question, CANDIDATES)
            dense_scored = (
                await dense(
                    session,
                    embedding,
                    MODEL,
                    VERSION,
                    CANDIDATES,
                    self.hardware.hnsw_ef_search,
                )
                if embedding
                else []
            )
            # Ids drive fusion, which never sees a magnitude; the scores travel separately
            # and end up in the query log. Keeping them apart is what stops a later change
            # from quietly making RRF scale-dependent.
            # The third signal, and it usually costs nothing: it returns immediately
            # unless the question contains something identifier-shaped. F15 measured the
            # case it exists for — a chunk holding the exact identifier ranked 52nd by
            # `ts_rank_cd`, two places outside the candidate set, because frequency
            # ranking has no notion of how rare a term is.
            exact_scored = await exact(session, question)
            lexical_ids = [chunk_id for chunk_id, _ in lexical_scored]
            dense_ids = [chunk_id for chunk_id, _ in dense_scored]
            exact_ids = [chunk_id for chunk_id, _ in exact_scored]
            # The union goes to the reranker; the fused top-k is what answers without one.
            # Choosing candidates and ordering results are different jobs, and RRF is only
            # good at the second.
            reranking = self.reranker is not None and self.hardware.rerank_candidates > 0
            ranked = (
                candidates(lexical_ids, dense_ids, exact_ids)[: self.hardware.rerank_candidates]
                if reranking
                else fuse(lexical_ids, dense_ids, limit, exact_ids)
            )
            hits = await hydrate(
                session, ranked, lexical_ids, dense_ids, dict(lexical_scored), dict(dense_scored)
            )

        rerank_reason: str | None = None
        if reranking and hits:
            hits, rerank_reason = await self._rerank(question, hits, limit)
        else:
            hits = hits[:limit]

        degraded_reason = degraded_reason or rerank_reason
        took = int((time.perf_counter() - started) * 1000)
        log.info(
            "search",
            lexical=len(lexical_ids),
            dense=len(dense_ids),
            exact=len(exact_ids),
            returned=len(hits),
            degraded=bool(degraded_reason),
            took_ms=took,
        )
        return SearchResult(
            hits=hits, degraded=bool(degraded_reason), reason=degraded_reason, took_ms=took
        )

    async def _rerank(
        self, question: str, hits: list[Hit], limit: int
    ) -> tuple[list[Hit], str | None]:
        """Reorder by what the cross-encoder read, or say why we could not.

        A reranker that is down must not take a working search away from the customer. The
        fused order is still a good order — it was the whole product one commit ago — so the
        answer degrades to it and the response says so.
        """
        if self.reranker is None:
            # Configured off. Not a degradation: the customer chose this profile and
            # `zenith diagnose` names what it disabled.
            return hits[:limit], None

        if not self.breaker.allows():
            # F11 measured why this exists: a reranker that times out costs the full
            # 5-second timeout on *every* query and returns the fused order anyway —
            # 6,237 ms for an answer that takes 1,152 ms with reranking switched off. The
            # timeout protects correctness and does nothing for latency. Skipping it while
            # the circuit is open turns a permanent tax into a one-minute one.
            #
            # Still reported as degraded, because it is: the answer is the fused order and
            # the customer paid for better.
            return hits[:limit], "reranking unavailable (circuit open); fused order"

        try:
            scored = await self.reranker.rank(question, [hit.text for hit in hits])
        except Exception as exc:  # noqa: BLE001 - degrading is the point
            self.breaker.failed()
            log.warning("rerank_failed", error=str(exc))
            return hits[:limit], f"reranking unavailable ({type(exc).__name__}); fused order"

        self.breaker.succeeded()

        # `replace` rather than mutation: `Hit` is frozen, and the cross-encoder's score is
        # the fourth column `query_citations` was designed to hold.
        return [replace(hits[item.index], rerank_score=item.score) for item in scored[:limit]], None

    async def _embed(self, question: str) -> tuple[list[float], str | None]:
        """The dense half's input, or an honest admission that we could not get it.

        Every failure here is survivable, because the lexical half still works. What is not
        survivable is pretending: a search that quietly drops semantic matching returns
        plausible results and hides that it is doing half its job.
        """
        try:
            return await self.embedder.embed_query(question), None
        except Exception as exc:  # noqa: BLE001 - degrading is the point
            log.warning("search_embedding_failed", error=str(exc))
            return [], f"semantic search unavailable ({type(exc).__name__}); lexical only"

    def _narrowed(self, labels: list[UUID] | None) -> TenantContext:
        """A caller may filter down to a subset of what they reach. Never up.

        A label the caller does not hold is a 403 rather than an empty result: the request
        is nonsense rather than unlucky, and answering it with silence would teach a client
        to retry with other people's labels to see which ones return nothing.
        """
        if not labels:
            return self.context
        beyond = [str(label) for label in labels if not self.context.reaches(label)]
        if beyond:
            raise PermissionDeniedError(f"you do not hold label(s): {', '.join(beyond)}")
        return TenantContext.for_tenant(self.context.tenant_id, labels)
