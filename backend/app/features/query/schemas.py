from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class TurnRequest(BaseModel):
    """One exchange the client already has on screen.

    Sent by the client rather than read from `queries` on purpose. A thread is what *this
    conversation* said, and the stored history is every question the user ever asked,
    interleaved across tabs and screens — rebuilding a thread from it would put a question
    asked ten minutes ago in a different tab into the context of this one.
    """

    question: str = Field(min_length=1, max_length=4000)
    answer: str = Field(max_length=8000)


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    labels: list[UUID] | None = Field(
        default=None, description="Narrow to a subset of the labels you reach."
    )
    #: The conversation so far, oldest first. Bounded again server-side — this cap only
    #: stops an oversized request body; what actually reaches the model is decided by
    #: `generation.conversation.bounded`, so a client cannot enlarge the prompt by sending
    #: more.
    history: list[TurnRequest] = Field(default_factory=list[TurnRequest], max_length=50)
    #: Answer from these documents and nothing else — what the chat box's `@` mentions
    #: send. A narrowing filter on top of the policies, never instead of them: an id the
    #: caller cannot read is a 403 rather than an empty answer, because "nothing found in
    #: that document" and "you may not read that document" are different statements and
    #: only one of them is about the corpus.
    #:
    #: Capped because every id is a parameter in the retrieval query and a mention list
    #: longer than this is not a person scoping a question.
    documents: list[UUID] | None = Field(
        default=None,
        max_length=20,
        description="Restrict retrieval to these documents.",
    )


class CitationResponse(BaseModel):
    """A citation is not the string "page 34" (mvp.md 2.9).

    Clicking it opens the document at the passage with the chunk highlighted, which is why
    the geometry travels with the marker. `marker` is the number as it appears in the answer
    text, so the viewer can tie the highlight to the bracket the user clicked.

    **Two kinds of geometry, because there are two kinds of document.** A PDF citation is a
    page and rectangles on it; a text citation is a character range. `media_type` says which
    of them to read, and the other is empty rather than invented.
    """

    marker: int
    chunk_id: UUID
    document_id: UUID
    filename: str
    media_type: str
    page_num: int | None
    char_start: int
    char_end: int
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
    page_num: int | None


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
