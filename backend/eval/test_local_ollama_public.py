"""Opt-in real local generator through retrieval and application-role PostgreSQL.

Only synthetic test source is used. This is a safety/adapter integration check,
not an answer-quality qualification. Run with ZENITH_OLLAMA_PUBLIC_EVAL=true.
"""

import json
import os
from uuid import UUID

import pytest

from app.features.generation.adapters.openai_compatible import OpenAIProvider
from app.features.generation.service import AnswerService
from app.features.retrieval.service import SearchService
from app.features.retrieval.tests.test_search import profile_for, seed
from conftest import Account, WorkingEmbedder

MODEL = "llama3.1:8b-instruct-q4_K_M"
ENDPOINT = "http://127.0.0.1:18084/v1"


@pytest.mark.skipif(
    os.environ.get("ZENITH_OLLAMA_PUBLIC_EVAL") != "true",
    reason="local GPU model service is opt-in",
)
async def test_local_model_retrieval_answer_and_abstention(account: Account) -> None:
    document = await seed(account.tenant_id, account.default_label)
    profile = await profile_for(account)
    provider = OpenAIProvider(ENDPOINT, MODEL)
    service = AnswerService(
        profile,
        provider=provider,
        search=SearchService(profile, embedder=WorkingEmbedder()),  # type: ignore[arg-type]
    )
    cases = (
        "What must controllers implement?",
        "What is the fine in euros?",
    )
    for question in cases:
        found = await service.search.search(question, documents=[document])
        print(
            json.dumps(
                {"question": question, "retrieved": len(found.hits), "reason": found.reason}
            ),
            flush=True,
        )
        answer = await service.answer(question, documents=[document])
        print(
            json.dumps(
                {
                    "question": question,
                    "answer": answer.answer,
                    "abstained": answer.abstained,
                    "citations": len(answer.citations),
                    "reason": answer.reason,
                    "model": answer.model,
                    "generation_ms": answer.took_generation_ms,
                }
            ),
            flush=True,
        )
        if found.hits:
            assert answer.model == MODEL
        else:
            assert answer.model == "" and answer.abstained and not answer.citations
        assert all(citation.document_id == UUID(str(document)) for citation in answer.citations)
        assert not answer.citations or not answer.abstained
