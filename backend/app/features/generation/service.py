"""Question in, cited answer out — and the log that explains it six months later.

The order matters and it is not the obvious one. The model runs *outside* the database
transaction, deliberately: an 8B model on CPU can take a minute to write a paragraph, and
holding a Postgres transaction open for that long pins a connection from a pool of ten and
blocks whatever `statement_timeout` was supposed to protect. Retrieval commits, the model
thinks, and the log is written afterwards in its own short transaction.
"""

import time
from dataclasses import dataclass
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.llm import BaseLLMProvider, ChunkCitation
from app.core.database import tenant_session
from app.features.auth.permissions import CATALOGUE
from app.features.auth.service import AccessProfile
from app.features.generation import citations as binding
from app.features.generation import prompt, providers
from app.features.generation.crypto import decrypt
from app.features.retrieval.search import Hit
from app.features.retrieval.service import SearchService

log = structlog.get_logger()

EXECUTE = "query.execute"
assert EXECUTE in CATALOGUE, "the permission this service is gated on must exist"

# How many passages the model reads. Not the same number as the search endpoint's default,
# and not a coincidence: every passage is ~1,200 characters of context that a local 8B model
# pays for in latency, and F7 measured Recall@8 at 95% — the answer is almost always in the
# first few. Asking for forty would buy 0% more recall and a much slower answer.
PASSAGES = 8


@dataclass(frozen=True, slots=True)
class Answer:
    query_id: UUID
    answer: str
    citations: list[ChunkCitation]
    abstained: bool
    # The passages consulted, whether or not any were cited. mvp.md 2.10: an abstention
    # says which documents were looked at — "I found nothing" and "I looked at nothing"
    # are different statements, and only one of them is about the corpus.
    consulted: list[Hit]
    model: str
    degraded: bool
    reason: str | None
    took_retrieval_ms: int
    took_generation_ms: int


class AnswerService:
    def __init__(
        self,
        profile: AccessProfile,
        provider: BaseLLMProvider | None = None,
        search: SearchService | None = None,
    ) -> None:
        self.profile = profile
        self.context = profile.context
        self.search = search or SearchService(profile)
        self._provider = provider

    async def answer(self, question: str, labels: list[UUID] | None = None) -> Answer:
        found = await self.search.search(question, PASSAGES, labels)
        provider = self._provider or await self._resolve()

        if not found.hits:
            # Nothing retrieved, so nothing to ground an answer in. Calling the model here
            # would be asking it to write from its own knowledge of the subject, which is
            # the one thing rule 1 of the prompt forbids — and it would cost a minute to
            # produce something this system must then refuse to show.
            bound = binding.Bound(prompt.ABSTENTION, [], abstained=True, fabricated=0)
            model, generation_ms = "", 0
        else:
            started = time.perf_counter()
            completion = await provider.complete(prompt.SYSTEM, prompt.build(question, found.hits))
            generation_ms = int((time.perf_counter() - started) * 1000)
            model = completion.model
            bound = binding.bind(completion.text, found.hits)

        query_id = await self._record(
            question, bound, found.hits, model, found.took_ms, generation_ms
        )
        log.info(
            "query",
            query_id=str(query_id),
            passages=len(found.hits),
            cited=len(bound.citations),
            abstained=bound.abstained,
            fabricated=bound.fabricated,
            degraded=found.degraded,
            retrieval_ms=found.took_ms,
            generation_ms=generation_ms,
        )
        return Answer(
            query_id=query_id,
            answer=bound.answer,
            citations=bound.citations,
            abstained=bound.abstained,
            consulted=found.hits,
            model=model,
            degraded=found.degraded,
            reason=found.reason,
            took_retrieval_ms=found.took_ms,
            took_generation_ms=generation_ms,
        )

    async def _resolve(self) -> BaseLLMProvider:
        """The tenant's own configuration, or the installation's, built by the registry.

        This method reads a row and returns a `Configuration`; `providers.build` decides
        what class that becomes. The split is what keeps provider selection in one place —
        otherwise "which adapter runs" would be answered here for tenants and in settings
        for everyone else, and the two would drift.

        Read inside `tenant_session`, so the policy `tenant_id = zenith_current_tenant()`
        does the scoping and the RLS bypass surface stays at the four routes 5.1 names.
        There is nothing here a customer's own session may not read — it is their
        configuration.
        """
        async with tenant_session(self.context) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT endpoint_url, model_name, api_key_encrypted FROM llm_config LIMIT 1"
                    )
                )
            ).first()

        if row is None:
            return providers.build(providers.from_settings())

        return providers.build(
            providers.Configuration(
                # A tenant configures an endpoint and a model, never an adapter: which
                # adapter speaks to that endpoint is an operator's decision about the
                # installation, not a customer's about their account.
                provider=providers.from_settings().provider,
                endpoint_url=row.endpoint_url,
                model=row.model_name,
                api_key=decrypt(row.api_key_encrypted) if row.api_key_encrypted else None,
            )
        )

    async def _record(
        self,
        question: str,
        bound: binding.Bound,
        hits: list[Hit],
        model: str,
        retrieval_ms: int,
        generation_ms: int,
    ) -> UUID:
        """The query log — observability now, the audit trail in iteration 4 (mvp.md 2.13).

        Written through the tenant's own session, so a row that would violate the policy
        cannot be inserted at all: `WITH CHECK` rejects a `tenant_id` other than the
        caller's. The value is passed from the context rather than from anything the request
        supplied.
        """
        async with tenant_session(self.context) as session:
            query_id = await session.scalar(
                text(
                    "INSERT INTO queries (tenant_id, user_id, question, answer, model_used, "
                    "latency_retrieval_ms, latency_generation_ms) "
                    "VALUES (:tenant, :user, :question, :answer, :model, :retrieval, "
                    ":generation) RETURNING id"
                ),
                {
                    "tenant": self.context.tenant_id,
                    "user": self.profile.user_id,
                    "question": question,
                    "answer": bound.answer,
                    "model": model or None,
                    "retrieval": retrieval_ms,
                    "generation": generation_ms,
                },
            )
            assert query_id is not None
            await _record_citations(session, UUID(str(query_id)), bound.citations, hits)
        return UUID(str(query_id))


async def _record_citations(
    session: AsyncSession, query_id: UUID, citations: list[ChunkCitation], hits: list[Hit]
) -> None:
    """Only the passages the answer actually used, with the scores that retrieved them.

    Only the cited ones, because the column is `query_citations` and a citation is what the
    answer leaned on — logging all eight would make the table describe the shortlist rather
    than the answer, and the shortlist is already reconstructable from the question.

    `rank` is the **retrieval** rank, not the order the markers appear in the prose. The
    other four columns in the row describe how the passage was retrieved, so the rank beside
    them has to mean the same thing.
    """
    if not citations:
        return

    ranks = {hit.chunk_id: position for position, hit in enumerate(hits, start=1)}
    by_id = {hit.chunk_id: hit for hit in hits}
    await session.execute(
        text(
            "INSERT INTO query_citations (query_id, chunk_id, rank, score_bm25, "
            "score_vector, score_rrf, score_rerank) "
            "VALUES (:query, :chunk, :rank, :bm25, :vector, :rrf, :rerank)"
        ),
        [
            {
                "query": query_id,
                "chunk": citation.chunk_id,
                "rank": ranks[citation.chunk_id],
                # Named `score_bm25` by the schema, holding `ts_rank_cd` by the
                # implementation. F6 chose Postgres full-text over pg_search's BM25; the
                # column keeps the name because renaming one across a migration to fix a
                # word is not worth the risk. Recorded in the F8 spec.
                "bm25": by_id[citation.chunk_id].lexical_score,
                "vector": by_id[citation.chunk_id].dense_score,
                "rrf": by_id[citation.chunk_id].score,
                "rerank": by_id[citation.chunk_id].rerank_score,
            }
            for citation in citations
        ],
    )
