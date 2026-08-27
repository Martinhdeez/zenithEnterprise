from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.features.auth.access.dependencies import CurrentProfile, requires
from app.features.retrieval.schemas import HitResponse, SearchResponse
from app.features.retrieval.service import DEFAULT_LIMIT, EXECUTE, MAX_LIMIT, SearchService

router = APIRouter(tags=["search"])


@router.get("/search", dependencies=[Depends(requires(EXECUTE))])
async def search(
    profile: CurrentProfile,
    q: Annotated[str, Query(min_length=1, max_length=1000, description="The question.")],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    labels: Annotated[
        list[UUID] | None, Query(description="Narrow to a subset of the labels you reach.")
    ] = None,
    documents: Annotated[
        list[UUID] | None, Query(description="Restrict the search to these documents.")
    ] = None,
) -> SearchResponse:
    """Hybrid search over the passages this caller is allowed to read.

    No tenant or label parameter decides what is visible — RLS does, inside the transaction.
    `labels` can only narrow the caller's own reach, and asking for one they do not hold is
    a 403 rather than an empty result.

    A missing embedding service is not an error: the lexical half answers alone and
    `degraded` says so. That is the honest half-answer, and it is what makes the response
    trustworthy when the box is busy ingesting.
    """
    result = await SearchService(profile).search(q, limit, labels, documents)
    return SearchResponse(
        hits=[HitResponse(**asdict(hit)) for hit in result.hits],
        degraded=result.degraded,
        reason=result.reason,
        took_ms=result.took_ms,
    )
