from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    labels: list[UUID] | None = Field(
        default=None, description="Narrow to a subset of the labels you reach."
    )


class CitationResponse(BaseModel):
    """A citation is not the string "page 34" (mvp.md 2.9).

    Clicking it opens the PDF on that page with the chunk highlighted, which is why the
    boxes and the page travel with the marker. `marker` is the number as it appears in the
    answer text, so the viewer can tie the highlight to the bracket the user clicked.
    """

    marker: int
    chunk_id: UUID
    document_id: UUID
    filename: str
    page_num: int
    text: str
    bboxes: list[dict[str, float]]


class ConsultedResponse(BaseModel):
    """What was read, cited or not.

    Present on abstentions above all: mvp.md 2.10 requires that a system finding no answer
    says so *and* shows what it looked at. "I found nothing" and "I looked at nothing" are
    different statements and only one of them is about the corpus.
    """

    document_id: UUID
    filename: str
    page_num: int


class QueryResponse(BaseModel):
    query_id: UUID
    answer: str
    citations: list[CitationResponse]
    # An abstention is a designed outcome, not an error, and the client needs to render it
    # as one. It arrives with 200 and this flag rather than as a 404.
    abstained: bool
    consulted: list[ConsultedResponse]
    model: str
    # From the search underneath: the embedder or the reranker being unreachable narrows
    # what the model was given to read, and that changes how much the answer is worth.
    degraded: bool
    reason: str | None
    took_retrieval_ms: int
    took_generation_ms: int


class HistoryEntryResponse(BaseModel):
    query_id: UUID
    question: str
    answer: str | None
    model_used: str | None
    #: How many passages the answer leaned on. A count rather than the citations
    #: themselves: a history list is a list, and fetching every citation for every row
    #: would make the cheap screen expensive.
    citations: int
    latency_retrieval_ms: int | None
    latency_generation_ms: int | None
    created_at: datetime
    #: Whose question this was. A shared history is only readable if you can tell.
    mine: bool


class HistoryResponse(BaseModel):
    entries: list[HistoryEntryResponse]
    #: Opaque. Pass it back as `cursor` for the next page; `null` means this is the last.
    next_cursor: str | None
