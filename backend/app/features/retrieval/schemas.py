from uuid import UUID

from pydantic import BaseModel


class HitResponse(BaseModel):
    """One passage, with everything the citation viewer and a future debugger both need.

    `lexical_rank` and `dense_rank` are positions, not scores, and either may be null — a
    hit found by only one half is the normal case and the reason the system is hybrid. M0
    found two identifier questions that **only** the lexical half could answer.
    """

    chunk_id: UUID
    document_id: UUID
    filename: str
    page_num: int
    text: str
    bboxes: list[dict[str, float]]
    lexical_rank: int | None
    dense_rank: int | None
    score: float


class SearchResponse(BaseModel):
    """`degraded` is part of the contract, not diagnostics.

    When the embedding service cannot be reached in time, the lexical half still answers and
    the response says so. A silently halved search returns plausible results and hides that
    it did half the job, which is the failure mode this project keeps refusing.
    """

    hits: list[HitResponse]
    degraded: bool
    reason: str | None
    took_ms: int
