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

import asyncio
import time
from dataclasses import dataclass, replace
from uuid import UUID

import structlog
from sqlalchemy import text

from app.common.exceptions import ConflictError, PermissionDeniedError
from app.core.config import settings
from app.core.database import tenant_session
from app.core.hardware import Profile
from app.core.hardware import active as active_profile
from app.features.auth.access.permissions import CATALOGUE
from app.features.auth.service import AccessProfile, AuthService
from app.features.embeddings.client import TeiClient
from app.features.embeddings.space import active as active_space
from app.features.retrieval.breaker import Breaker
from app.features.retrieval.coverage import REPRESENTATION, VERSION, CoverageReceipt
from app.features.retrieval.degradation import (
    DIRECT_INCOMPLETE,
    EXTERNAL_JUDGE_UNAVAILABLE,
    RERANKING_UNAVAILABLE,
    SEMANTIC_UNAVAILABLE,
    SOURCE_CHANGED,
)
from app.features.retrieval.direct import DirectPlan, fingerprint, plan, verify_current
from app.features.retrieval.evidence_policy import POLICY_ID, EvidenceStatusV1
from app.features.retrieval.identifiers import exact
from app.features.retrieval.judging.jev import (
    JevFailure,
    Purpose,
    configured_jev_judge,
)
from app.features.retrieval.judging.protocol import (
    AssessmentBatch,
    Candidate,
    CompletionState,
    Judge,
    Outcome,
)
from app.features.retrieval.judging.rubrics import Formulation
from app.features.retrieval.judging.tei import TeiJudge
from app.features.retrieval.relevance import Relevance, best_rerank, classify
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
    #: How much the corpus has to say about this question. Held apart from `degraded` on
    #: purpose: `degraded` means a component was missing, this means the corpus was.
    #: Conflating them would tell a customer their installation is broken when their archive
    #: simply does not cover the question.
    relevance: Relevance = Relevance.CONFIDENT
    assessment: AssessmentBatch | None = None
    requested_provider: str = "tei"
    fallback_provider: str | None = None
    evidence_status: EvidenceStatusV1 | None = None
    evidence_policy: str | None = None
    receipt: CoverageReceipt | None = None


class SearchService:
    def __init__(
        self,
        profile: AccessProfile,
        embedder: TeiClient | None = None,
        hardware: Profile | None = None,
        reranker: TeiReranker | None = None,
        breaker: Breaker | None = None,
        judge: Judge | None = None,
        judge_mode: str | None = None,
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
        self.judge = (
            judge
            if judge is not None
            else (TeiJudge(self.reranker) if self.reranker is not None else None)
        )
        self.judge_mode = judge_mode or (
            "tei" if judge is not None else settings.evidence_judge_provider
        )

    async def search(
        self,
        question: str,
        limit: int = DEFAULT_LIMIT,
        labels: list[UUID] | None = None,
        documents: list[UUID] | None = None,
        mode: str = "legacy",
    ) -> SearchResult:
        started = time.perf_counter()
        if mode not in {"legacy", "hybrid", "auto", "direct"}:
            raise ValueError("unknown retrieval mode")
        if mode in {"auto", "direct"} and not settings.direct_enabled:
            raise ConflictError("Direct retrieval is disabled by this installation.")
        limit = max(1, min(limit, MAX_LIMIT))
        context = self._narrowed(labels)
        documents = documents or None

        planned: DirectPlan | None = None
        direct_reason: str | None = None
        if mode in {"auto", "direct"}:
            try:
                remaining = settings.direct_total_deadline_seconds - (time.perf_counter() - started)
                async with asyncio.timeout(remaining):
                    if documents:
                        await self._reachable(documents)
                    planned = await plan(
                        context,
                        documents,
                        question,
                        max_units=settings.direct_max_units,
                        max_windows=settings.direct_max_windows,
                        max_source_bytes=settings.direct_max_source_bytes,
                        max_rendered_bytes=settings.direct_max_rendered_bytes,
                        window_chars=settings.direct_window_chars,
                        overlap_chars=settings.direct_window_overlap_chars,
                        max_pair_bytes=settings.direct_max_pair_bytes,
                    )
            except TimeoutError:
                planned = DirectPlan(False, "direct_deadline")
            if planned.complete:
                direct_result = await self._search_direct(
                    question, limit, context, documents, planned, started
                )
                if mode == "direct" or (
                    direct_result.receipt is not None
                    and direct_result.receipt.execution_status == "complete"
                ):
                    return direct_result
                direct_reason = (
                    direct_result.receipt.reason_codes[0]
                    if direct_result.receipt and direct_result.receipt.reason_codes
                    else "direct_assessment_incomplete"
                )
            else:
                direct_reason = planned.reason
                if mode == "direct":
                    return self._direct_unavailable(context, documents, planned, started)

        embedding, degraded_reason = await self._embed(question)

        if documents and mode not in {"auto", "direct"}:
            await self._reachable(documents)

        async with tenant_session(context) as session:
            lexical_scored = await lexical(session, question, CANDIDATES, documents)
            dense_scored = (
                await dense(
                    session,
                    embedding,
                    # Read here rather than taken from `embeddings.client`'s constants, and
                    # that is the change migration 0027 required. Which space is serving is a
                    # property of the *installation* — a reindex flips it while this process
                    # runs — and it now decides how the query vector is projected, not only
                    # which rows are eligible. A constant could be right about the name and
                    # wrong about the basis, which is the one mistake that ranks instead of
                    # failing.
                    await active_space(session),
                    CANDIDATES,
                    self.hardware.hnsw_ef_search,
                    documents,
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
            exact_scored = await exact(session, question, documents=documents)
            lexical_ids = [chunk_id for chunk_id, _ in lexical_scored]
            dense_ids = [chunk_id for chunk_id, _ in dense_scored]
            exact_ids = [chunk_id for chunk_id, _ in exact_scored]
            # The union goes to the reranker; the fused top-k is what answers without one.
            # Choosing candidates and ordering results are different jobs, and RRF is only
            # good at the second.
            reranking = self.judge is not None and self.hardware.rerank_candidates > 0
            ranked = (
                # The limit goes *into* `candidates`, not around it. Slicing afterwards
                # discards the promoted leaders and turns this back into a plain RRF
                # top-N — see `candidates`.
                candidates(lexical_ids, dense_ids, exact_ids, self.hardware.rerank_candidates)
                if reranking
                else fuse(lexical_ids, dense_ids, limit, exact_ids)
            )
            hits = await hydrate(
                session, ranked, lexical_ids, dense_ids, dict(lexical_scored), dict(dense_scored)
            )

        rerank_reason: str | None = None
        assessment: AssessmentBatch | None = None
        fallback_provider: str | None = None
        if reranking and hits:
            if self.judge_mode.startswith("jev_"):
                hits, rerank_reason, assessment, fallback_provider = await self._rerank_jev(
                    question, hits, limit, context, documents
                )
            else:
                hits, rerank_reason, assessment = await self._rerank(question, hits, limit)
        else:
            hits = hits[:limit]

        degraded_reason = degraded_reason or rerank_reason
        if (
            mode != "legacy"
            and hits
            and not await verify_current(self.profile, context, tuple(hits))
        ):
            hits = []
            degraded_reason = SOURCE_CHANGED
        # Read after reranking, from what is actually being returned. The best passage only:
        # a set whose top hit plainly answers the question is a good set even if the eighth
        # is noise, and averaging would let seven weak passages outvote the one that is right.
        provider_aware = assessment is not None and assessment.provider == "jev"
        unassessed = (
            provider_aware
            or rerank_reason == SOURCE_CHANGED
            or mode != "legacy"
            or (
                self.judge_mode.startswith("jev_")
                and (assessment is None or assessment.provider != "tei")
            )
        )
        relevance = (
            Relevance.NOT_ASSESSED
            if unassessed
            else classify(
                best_rerank([hit.rerank_score for hit in hits]),
                sum(1 for hit in hits if hit.lexical_rank is not None),
                len(hits),
            )
        )
        if relevance is Relevance.NONE:
            # Withheld rather than ranked. Showing them under a notice saying they do not
            # match would be asking the reader to disbelieve what is on their own screen.
            hits = []
        took = int((time.perf_counter() - started) * 1000)
        log.info(
            "search",
            lexical=len(lexical_ids),
            dense=len(dense_ids),
            exact=len(exact_ids),
            scoped=len(documents or []),
            returned=len(hits),
            degraded=bool(degraded_reason),
            relevance=relevance.value,
            judge_provider=assessment.provider if assessment else None,
            judge_model=assessment.reported_model if assessment else None,
            judge_score_kind=(
                assessment.judgments[0].score_kind.value
                if assessment and assessment.judgments and assessment.judgments[0].score_kind
                else None
            ),
            requested_judge_provider=self.judge_mode if reranking else "none",
            fallback_provider=fallback_provider,
            evidence_status=EvidenceStatusV1.NOT_ASSESSED.value if unassessed else None,
            took_ms=took,
        )
        return SearchResult(
            hits=hits,
            degraded=bool(degraded_reason),
            reason=degraded_reason,
            took_ms=took,
            relevance=relevance,
            assessment=assessment,
            requested_provider=self.judge_mode if reranking else "none",
            fallback_provider=fallback_provider,
            evidence_status=EvidenceStatusV1.NOT_ASSESSED if unassessed else None,
            evidence_policy=POLICY_ID if unassessed else None,
            receipt=(
                self._hybrid_receipt(
                    context,
                    documents,
                    hits,
                    assessment,
                    degraded_reason,
                    fallback_provider,
                    direct_reason,
                )
                if mode != "legacy"
                else None
            ),
        )

    @staticmethod
    def _scope_identity(context: TenantContext, documents: list[UUID] | None) -> str:
        return fingerprint(
            [
                str(context.tenant_id),
                *sorted(str(label) for label in context.label_ids),
                "documents",
                *sorted(str(document) for document in documents or []),
            ]
        )

    def _direct_unavailable(
        self,
        context: TenantContext,
        documents: list[UUID] | None,
        planned: DirectPlan,
        started: float,
    ) -> SearchResult:
        receipt = CoverageReceipt(
            version=VERSION,
            strategy="direct",
            coverage_method="eligible_scope_manifest",
            execution_status="partial",
            scope_fingerprint=self._scope_identity(context, documents),
            manifest_fingerprint=planned.manifest_fingerprint,
            eligible_units=planned.eligible_units,
            selected_units=0,
            attempted_units=0,
            assessed_units=0,
            failed_units=0,
            skipped_units=0,
            assessment_windows=0,
            manifest_assessment_complete=False,
            source_representation=REPRESENTATION,
            snapshot_status="unknown",
            evidence_status=EvidenceStatusV1.NOT_ASSESSED.value,
            provider=None,
            model=None,
            rubric_id=None,
            degraded=True,
            fallback_provider=None,
            fallback_strategy=None,
            reason_codes=(planned.reason or "direct_unavailable",),
        )
        return SearchResult(
            hits=[],
            degraded=True,
            reason=DIRECT_INCOMPLETE,
            took_ms=int((time.perf_counter() - started) * 1000),
            relevance=Relevance.NOT_ASSESSED,
            requested_provider=self.judge_mode,
            evidence_status=EvidenceStatusV1.NOT_ASSESSED,
            evidence_policy=POLICY_ID,
            receipt=receipt,
        )

    def _hybrid_receipt(
        self,
        context: TenantContext,
        documents: list[UUID] | None,
        hits: list[Hit],
        batch: AssessmentBatch | None,
        reason: str | None,
        fallback_provider: str | None,
        direct_reason: str | None,
    ) -> CoverageReceipt:
        assessed = sum(item.outcome is Outcome.ASSESSED for item in batch.judgments) if batch else 0
        failed = sum(item.outcome is Outcome.FAILED for item in batch.judgments) if batch else 0
        skipped = sum(item.outcome is Outcome.SKIPPED for item in batch.judgments) if batch else 0
        complete = batch is not None and batch.completion_state is CompletionState.COMPLETE
        codes = tuple(
            code
            for code in (
                direct_reason,
                "source_changed" if reason == SOURCE_CHANGED else None,
                "ordering_degraded" if reason and reason != SOURCE_CHANGED else None,
            )
            if code
        )
        return CoverageReceipt(
            version=VERSION,
            strategy="hybrid",
            coverage_method="candidate_set",
            execution_status="complete" if complete else "partial",
            scope_fingerprint=self._scope_identity(context, documents),
            manifest_fingerprint=None,
            eligible_units=None,
            selected_units=len(hits),
            attempted_units=len(batch.requested_ids) if batch else 0,
            assessed_units=assessed,
            failed_units=failed,
            skipped_units=skipped,
            assessment_windows=len(batch.requested_ids) if batch else 0,
            manifest_assessment_complete=False,
            source_representation=REPRESENTATION,
            snapshot_status="changed" if reason == SOURCE_CHANGED else "unknown",
            evidence_status=EvidenceStatusV1.NOT_ASSESSED.value,
            provider=batch.provider if batch else None,
            model=batch.reported_model if batch else None,
            rubric_id=batch.judgments[0].rubric_id if batch and batch.judgments else None,
            degraded=bool(reason),
            fallback_provider=fallback_provider,
            fallback_strategy="hybrid" if direct_reason else None,
            reason_codes=codes,
        )

    async def _local_direct_input_safe(self, started: float) -> bool:
        """Fail early if the live model limit is too small or cannot be read."""
        if not isinstance(self.judge, TeiJudge):
            return self.judge is not None  # deterministic test/alternative judge
        if not isinstance(self.judge.reranker, TeiReranker):
            return False
        remaining = settings.direct_total_deadline_seconds - (time.perf_counter() - started)
        if remaining <= 0:
            return False
        try:
            async with asyncio.timeout(remaining):
                limit = await self.judge.reranker.max_input_length()
            # A rough early guard; truncate=False at dispatch is the final guarantee.
            return limit is not None and settings.direct_max_pair_bytes <= limit - 8
        except TimeoutError:
            return False

    async def _search_direct(
        self,
        question: str,
        limit: int,
        context: TenantContext,
        documents: list[UUID] | None,
        planned: DirectPlan,
        started: float,
    ) -> SearchResult:
        assert planned.complete
        if not planned.units:
            snapshot_issue = await self._direct_snapshot_current(
                question, context, documents, planned, started
            )
            if snapshot_issue:
                return self._direct_failed(context, documents, planned, started, snapshot_issue)
            receipt = CoverageReceipt(
                version=VERSION,
                strategy="direct",
                coverage_method="eligible_scope_manifest",
                execution_status="complete",
                scope_fingerprint=self._scope_identity(context, documents),
                manifest_fingerprint=planned.manifest_fingerprint,
                eligible_units=0,
                selected_units=0,
                attempted_units=0,
                assessed_units=0,
                failed_units=0,
                skipped_units=0,
                assessment_windows=0,
                manifest_assessment_complete=True,
                source_representation=REPRESENTATION,
                snapshot_status="unchanged",
                evidence_status=EvidenceStatusV1.NONE_FOUND.value,
                provider=None,
                model=None,
                rubric_id=None,
                degraded=False,
                fallback_provider=None,
                fallback_strategy=None,
            )
            return SearchResult(
                [],
                False,
                None,
                int((time.perf_counter() - started) * 1000),
                Relevance.NOT_ASSESSED,
                requested_provider=self.judge_mode,
                evidence_status=EvidenceStatusV1.NONE_FOUND,
                evidence_policy=POLICY_ID,
                receipt=receipt,
            )

        remaining = settings.direct_total_deadline_seconds - (time.perf_counter() - started)
        if remaining <= 0:
            return self._direct_failed(context, documents, planned, started, "direct_deadline")
        if not self.judge_mode.startswith("jev_"):
            safe = await self._local_direct_input_safe(started)
            if not safe:
                return self._direct_failed(
                    context, documents, planned, started, "direct_model_limit_unverified"
                )
        remaining = settings.direct_total_deadline_seconds - (time.perf_counter() - started)
        if remaining <= 0:
            return self._direct_failed(context, documents, planned, started, "direct_deadline")
        try:
            async with asyncio.timeout(remaining):
                authorized = await verify_current(self.profile, context, planned.units)
        except TimeoutError:
            return self._direct_failed(context, documents, planned, started, "direct_deadline")
        if not authorized:
            return self._direct_failed(
                context, documents, planned, started, "direct_state_unverified"
            )
        windows = {window.id: window for window in planned.windows}
        units = {hit.chunk_id: hit for hit in planned.units}
        candidates = [Candidate(window.id, window.text) for window in planned.windows]
        expected_ids = tuple(candidate.id for candidate in candidates)
        batch: AssessmentBatch | None = None
        fallback_provider: str | None = None
        assessment_failure: str | None = None
        remaining = settings.direct_total_deadline_seconds - (time.perf_counter() - started)
        try:
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                if self.judge_mode.startswith("jev_"):

                    async def permitted(query: str, candidate: Candidate, purpose: Purpose) -> bool:
                        window = windows.get(candidate.id)
                        return bool(
                            query == question
                            and purpose is Purpose.RERANKING
                            and window is not None
                            and candidate.text == window.text
                            and await verify_current(
                                self.profile, context, (units[window.chunk_id],)
                            )
                        )

                    formulation = (
                        Formulation.SCORE6 if self.judge_mode == "jev_score6" else Formulation.NOUL
                    )
                    client = configured_jev_judge(
                        formulation=formulation,
                        purpose=Purpose.RERANKING,
                        authorize=permitted,
                        deadline_seconds=remaining,
                    )
                    try:
                        batch = await client.assess(question, candidates)
                    finally:
                        await client.aclose()
                elif self.judge is not None:
                    local_judge = (
                        TeiJudge(self.judge.reranker, truncate=False)
                        if isinstance(self.judge, TeiJudge)
                        else self.judge
                    )
                    batch = await local_judge.assess(question, candidates)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            assessment_failure = "direct_deadline"
            batch = None
        except Exception:  # noqa: BLE001 - no provider/source-bearing exception is logged
            batch = None

        if batch is not None and batch.requested_ids != expected_ids:
            batch = None

        complete = (
            batch is not None
            and batch.completion_state is CompletionState.COMPLETE
            and all(item.outcome is Outcome.ASSESSED for item in batch.judgments)
        )
        if not complete and self.judge_mode.startswith("jev_") and self.judge is not None:
            remaining = settings.direct_total_deadline_seconds - (time.perf_counter() - started)
            try:
                if remaining <= 0:
                    raise TimeoutError
                async with asyncio.timeout(remaining):
                    local_judge = (
                        TeiJudge(self.judge.reranker, truncate=False)
                        if isinstance(self.judge, TeiJudge)
                        else self.judge
                    )
                    local = (
                        await local_judge.assess(question, candidates)
                        if await self._local_direct_input_safe(started)
                        else None
                    )
                if (
                    local is not None
                    and local.requested_ids == expected_ids
                    and local.completion_state is CompletionState.COMPLETE
                    and all(item.outcome is Outcome.ASSESSED for item in local.judgments)
                ):
                    batch = local
                    complete = True
                    fallback_provider = "tei"
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                assessment_failure = "direct_deadline"
            except Exception:  # noqa: BLE001 - partial remains explicit
                pass
        if not complete or batch is None:
            return self._direct_failed(
                context,
                documents,
                planned,
                started,
                assessment_failure or "direct_assessment_incomplete",
                batch,
            )
        snapshot_issue = await self._direct_snapshot_current(
            question, context, documents, planned, started
        )
        if snapshot_issue:
            return self._direct_failed(context, documents, planned, started, snapshot_issue, batch)
        input_order = {window.id: index for index, window in enumerate(planned.windows)}
        ordered = sorted(
            batch.judgments,
            key=lambda item: (
                -item.rank_value if item.rank_value is not None else float("inf"),
                input_order[item.candidate_id],
            ),
        )
        selected: list[Hit] = []
        seen: set[UUID] = set()
        for judgment in ordered:
            window = windows[judgment.candidate_id]
            if window.chunk_id in seen:
                continue
            seen.add(window.chunk_id)
            source = units[window.chunk_id]
            exact_offsets = source.char_end - source.char_start == len(source.text)
            selected.append(
                replace(
                    source,
                    text=window.text if exact_offsets else source.text,
                    char_start=source.char_start + window.start
                    if exact_offsets
                    else source.char_start,
                    char_end=source.char_start + window.end if exact_offsets else source.char_end,
                    bboxes=(
                        source.bboxes
                        if not exact_offsets
                        or (window.start == 0 and window.end == len(source.text))
                        else []
                    ),
                    rerank_score=judgment.rank_value,
                )
            )
            if len(selected) >= limit:
                break
        receipt = CoverageReceipt(
            version=VERSION,
            strategy="direct",
            coverage_method="eligible_scope_manifest",
            execution_status="complete",
            scope_fingerprint=self._scope_identity(context, documents),
            manifest_fingerprint=planned.manifest_fingerprint,
            eligible_units=len(planned.units),
            selected_units=len(selected),
            attempted_units=len(planned.units),
            assessed_units=len(planned.units),
            failed_units=0,
            skipped_units=0,
            assessment_windows=len(planned.windows),
            manifest_assessment_complete=True,
            source_representation=REPRESENTATION,
            snapshot_status="unchanged",
            evidence_status=EvidenceStatusV1.NOT_ASSESSED.value,
            provider=batch.provider,
            model=batch.reported_model,
            rubric_id=batch.judgments[0].rubric_id if batch.judgments else None,
            degraded=bool(fallback_provider),
            fallback_provider=fallback_provider,
            fallback_strategy=None,
            reason_codes=("local_fallback",) if fallback_provider else (),
        )
        return SearchResult(
            hits=selected,
            degraded=bool(fallback_provider),
            reason=EXTERNAL_JUDGE_UNAVAILABLE if fallback_provider else None,
            took_ms=int((time.perf_counter() - started) * 1000),
            relevance=Relevance.NOT_ASSESSED,
            assessment=batch,
            requested_provider=self.judge_mode,
            fallback_provider=fallback_provider,
            evidence_status=EvidenceStatusV1.NOT_ASSESSED,
            evidence_policy=POLICY_ID,
            receipt=receipt,
        )

    async def _direct_snapshot_current(
        self,
        question: str,
        context: TenantContext,
        documents: list[UUID] | None,
        planned: DirectPlan,
        started: float,
    ) -> str | None:
        """Reauthorize and detect additions, removals, or changed text before disclosure."""
        remaining = settings.direct_total_deadline_seconds - (time.perf_counter() - started)
        if remaining <= 0:
            return "direct_deadline"
        try:
            async with asyncio.timeout(remaining):
                authorized = await verify_current(self.profile, context, planned.units)
                current = await plan(
                    context,
                    documents,
                    question,
                    max_units=settings.direct_max_units,
                    max_windows=settings.direct_max_windows,
                    max_source_bytes=settings.direct_max_source_bytes,
                    max_rendered_bytes=settings.direct_max_rendered_bytes,
                    window_chars=settings.direct_window_chars,
                    overlap_chars=settings.direct_window_overlap_chars,
                    max_pair_bytes=settings.direct_max_pair_bytes,
                )
                if (
                    not current.complete
                    or current.eligible_units != planned.eligible_units
                    or current.manifest_fingerprint != planned.manifest_fingerprint
                ):
                    return "direct_snapshot_changed"
                return None if authorized else "direct_state_unverified"
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return "direct_deadline"
        except Exception:  # noqa: BLE001 - uncertainty is incomplete coverage
            return "direct_state_unverified"

    def _direct_failed(
        self,
        context: TenantContext,
        documents: list[UUID] | None,
        planned: DirectPlan,
        started: float,
        code: str,
        batch: AssessmentBatch | None = None,
    ) -> SearchResult:
        windows = {window.id: window for window in planned.windows}
        assessed_windows: set[UUID] = (
            {item.candidate_id for item in batch.judgments if item.outcome is Outcome.ASSESSED}
            if batch
            else set()
        )
        fully_assessed = sum(
            all(
                window.id in assessed_windows
                for window in planned.windows
                if window.chunk_id == hit.chunk_id
            )
            for hit in planned.units
        )
        receipt = CoverageReceipt(
            version=VERSION,
            strategy="direct",
            coverage_method="eligible_scope_manifest",
            execution_status="partial",
            scope_fingerprint=self._scope_identity(context, documents),
            manifest_fingerprint=planned.manifest_fingerprint,
            eligible_units=planned.eligible_units,
            selected_units=0,
            attempted_units=len({windows[item.candidate_id].chunk_id for item in batch.judgments})
            if batch
            else 0,
            assessed_units=fully_assessed,
            failed_units=len(
                {
                    windows[item.candidate_id].chunk_id
                    for item in batch.judgments
                    if item.outcome is Outcome.FAILED
                }
            )
            if batch
            else 0,
            skipped_units=len(
                {
                    windows[item.candidate_id].chunk_id
                    for item in batch.judgments
                    if item.outcome is Outcome.SKIPPED
                }
            )
            if batch
            else 0,
            assessment_windows=len(planned.windows),
            manifest_assessment_complete=False,
            source_representation=REPRESENTATION,
            snapshot_status="changed" if code == "direct_snapshot_changed" else "unknown",
            evidence_status=EvidenceStatusV1.NOT_ASSESSED.value,
            provider=batch.provider if batch else None,
            model=batch.reported_model if batch else None,
            rubric_id=batch.judgments[0].rubric_id if batch and batch.judgments else None,
            degraded=True,
            fallback_provider=None,
            fallback_strategy=None,
            reason_codes=(code,),
        )
        return SearchResult(
            hits=[],
            degraded=True,
            reason=SOURCE_CHANGED if code == "direct_snapshot_changed" else DIRECT_INCOMPLETE,
            took_ms=int((time.perf_counter() - started) * 1000),
            relevance=Relevance.NOT_ASSESSED,
            assessment=batch,
            requested_provider=self.judge_mode,
            evidence_status=EvidenceStatusV1.NOT_ASSESSED,
            evidence_policy=POLICY_ID,
            receipt=receipt,
        )

    async def _rerank(
        self, question: str, hits: list[Hit], limit: int
    ) -> tuple[list[Hit], str | None, AssessmentBatch | None]:
        """Reorder by what the cross-encoder read, or say why we could not.

        A reranker that is down must not take a working search away from the customer. The
        fused order is still a good order — it was the whole product one commit ago — so the
        answer degrades to it and the response says so.
        """
        if self.judge is None:
            # Configured off. Not a degradation: the customer chose this profile and
            # `zenith diagnose` names what it disabled.
            return hits[:limit], None, None

        if not self.breaker.allows():
            # F11 measured why this exists: a reranker that times out costs the full
            # 5-second timeout on *every* query and returns the fused order anyway —
            # 6,237 ms for an answer that takes 1,152 ms with reranking switched off. The
            # timeout protects correctness and does nothing for latency. Skipping it while
            # the circuit is open turns a permanent tax into a one-minute one.
            #
            # Still reported as degraded, because it is: the answer is the fused order and
            # the customer paid for better.
            #
            # The circuit being open is a fact about this installation, not about the
            # reader's results, so it goes to the log and the sentence they see says what
            # actually changed for them. See `degradation.py`.
            log.info("rerank_skipped", cause="circuit_open")
            return hits[:limit], RERANKING_UNAVAILABLE, None

        try:
            batch = await self.judge.assess(
                question, [Candidate(hit.chunk_id, hit.text) for hit in hits]
            )
        except Exception as exc:  # noqa: BLE001 - degrading is the point
            self.breaker.failed()
            # The exception name stays here, where somebody who can fix it will look. It used
            # to travel to the screen as well: `reranking unavailable (ReadTimeout)`.
            log.warning("rerank_failed", error=str(exc), cause=type(exc).__name__)
            return hits[:limit], RERANKING_UNAVAILABLE, None

        if batch.completion_state is not CompletionState.COMPLETE or any(
            item.outcome is not Outcome.ASSESSED for item in batch.judgments
        ):
            self.breaker.failed()
            log.warning("rerank_incomplete", provider=batch.provider)
            return hits[:limit], RERANKING_UNAVAILABLE, batch

        self.breaker.succeeded()

        # `replace` rather than mutation: `Hit` is frozen, and the cross-encoder's score is
        # the fourth column `query_citations` was designed to hold.
        by_id = {hit.chunk_id: hit for hit in hits}
        ordered = sorted(
            batch.judgments,
            key=lambda item: -item.rank_value if item.rank_value is not None else float("inf"),
        )
        return (
            [
                replace(by_id[item.candidate_id], rerank_score=item.rank_value)
                for item in ordered[:limit]
            ],
            None,
            batch,
        )

    async def _rerank_jev(
        self,
        question: str,
        hits: list[Hit],
        limit: int,
        context: TenantContext,
        documents: list[UUID] | None,
    ) -> tuple[list[Hit], str | None, AssessmentBatch | None, str | None]:
        """Rank one comparable set; any failure uses a complete local ordering."""
        started = time.monotonic()
        budget = settings.jev_total_deadline_seconds
        jev_budget = max(0.1, budget - 5.0)
        by_id = {hit.chunk_id: hit for hit in hits}

        async def permitted(query: str, candidate: Candidate, purpose: Purpose) -> bool:
            if query != question or purpose is not Purpose.RERANKING:
                return False
            hit = by_id.get(candidate.id)
            if hit is None or hit.text != candidate.text:
                return False
            try:
                # Fresh role/label resolution, then the *original narrowed* scope. This
                # is a short application-role read, closed before inference starts.
                current = await AuthService().profile(self.profile.user_id, context.tenant_id)
                if EXECUTE not in current.permissions:
                    return False
                labels = set(context.label_ids).intersection(current.context.label_ids)
                narrowed = TenantContext.for_tenant(context.tenant_id, labels)
                async with tenant_session(narrowed) as session:
                    row = await session.execute(
                        text(
                            "SELECT c.text, c.document_id, d.sha256 FROM chunks c "
                            "JOIN documents d ON d.id = c.document_id "
                            "WHERE c.id = :id AND d.status = 'ready'"
                        ),
                        {"id": candidate.id},
                    )
                    source = row.one_or_none()
                return bool(
                    source is not None
                    and source.text == candidate.text
                    and source.document_id == hit.document_id
                    and hit.source_sha256 is not None
                    and source.sha256 == hit.source_sha256
                    and (not documents or source.document_id in documents)
                )
            except Exception:  # noqa: BLE001 - authorization errors deny export/disclosure
                return False

        batch: AssessmentBatch | None = None
        try:
            formulation = (
                Formulation.SCORE6 if self.judge_mode == "jev_score6" else Formulation.NOUL
            )
            client = configured_jev_judge(
                formulation=formulation,
                purpose=Purpose.RERANKING,
                authorize=permitted,
                deadline_seconds=jev_budget,
            )
            try:
                batch = await client.assess(
                    question, [Candidate(hit.chunk_id, hit.text) for hit in hits]
                )
            finally:
                await client.aclose()
        except asyncio.CancelledError:
            raise
        except JevFailure as exc:
            log.info("experimental_judge_unavailable", cause=exc.code)
        except Exception:  # noqa: BLE001 - no source-bearing exception reaches the log
            log.warning("experimental_judge_unavailable", cause="internal_error")

        if (
            batch is not None
            and batch.completion_state is CompletionState.COMPLETE
            and all(item.outcome is Outcome.ASSESSED for item in batch.judgments)
        ):
            ordered = sorted(
                batch.judgments,
                key=lambda item: -item.rank_value if item.rank_value is not None else float("inf"),
            )
            selected = [
                replace(by_id[item.candidate_id], rerank_score=item.rank_value)
                for item in ordered[:limit]
            ]
            if all(
                [
                    await permitted(question, Candidate(hit.chunk_id, hit.text), Purpose.RERANKING)
                    for hit in selected
                ]
            ):
                return selected, None, batch, None
            return [], SOURCE_CHANGED, batch, None

        remaining = budget - (time.monotonic() - started)
        if self.judge is not None and remaining > 0:
            try:
                async with asyncio.timeout(remaining):
                    fallback, reason, local_batch = await self._rerank(question, hits, limit)
                if reason is None and local_batch is not None:
                    # Recheck even local fallback because the Jev attempt may have queued.
                    if all(
                        [
                            await permitted(
                                question, Candidate(hit.chunk_id, hit.text), Purpose.RERANKING
                            )
                            for hit in fallback
                        ]
                    ):
                        return fallback, EXTERNAL_JUDGE_UNAVAILABLE, local_batch, "tei"
                    return [], SOURCE_CHANGED, local_batch, "tei"
            except TimeoutError:
                pass
        selected = hits[:limit]
        if not all(
            [
                await permitted(question, Candidate(hit.chunk_id, hit.text), Purpose.RERANKING)
                for hit in selected
            ]
        ):
            return [], SOURCE_CHANGED, batch, None
        return selected, RERANKING_UNAVAILABLE, batch, None

    async def _embed(self, question: str) -> tuple[list[float], str | None]:
        """The dense half's input, or an honest admission that we could not get it.

        Every failure here is survivable, because the lexical half still works. What is not
        survivable is pretending: a search that quietly drops semantic matching returns
        plausible results and hides that it is doing half its job.
        """
        try:
            return await self.embedder.embed_query(question), None
        except Exception as exc:  # noqa: BLE001 - degrading is the point
            log.warning("search_embedding_failed", error=str(exc), cause=type(exc).__name__)
            return [], SEMANTIC_UNAVAILABLE

    async def _reachable(self, documents: list[UUID]) -> None:
        """Scoping to a document you cannot open is a 403, not an empty answer.

        The filter itself is already safe — it narrows a set the policies produced, so an
        unreachable id can only ever match nothing. This check is about what the caller is
        told. A scoped question that silently returns nothing reads as *"that fact is not in
        this document"*, which is a claim about the corpus rather than about permissions,
        and it is the wrong one.

        It leaks nothing: the lookup runs inside a `tenant_session`, so a document in another
        tenant and a document that does not exist are indistinguishable here, and both
        produce the same message. That is the same trade `_narrowed` makes for labels.

        **Against the caller's full reach, not the narrowed context.** Narrowing to `finance`
        and naming a document filed under `default` is a contradiction rather than a
        permission problem — the caller can read that document and has just asked to look
        away from it — so the two filters compose and the answer is empty. Checking inside
        the narrowed context would report that as a 403 and tell them they cannot read a
        document they can.
        """
        async with tenant_session(self.context) as session:
            found = set(
                await session.scalars(
                    text("SELECT id FROM documents WHERE id = ANY(:ids)"),
                    {"ids": [str(document) for document in documents]},
                )
            )
        beyond = [str(document) for document in documents if document not in found]
        if beyond:
            raise PermissionDeniedError(f"you cannot read document(s): {', '.join(sorted(beyond))}")

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
