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
    #: Which viewer opens this citation. Sent rather than inferred from the filename: the
    #: client would have to guess, and `notes.pdf.txt` is the guess going wrong.
    media_type: str
    #: `None` where the document has no pages. Not `1`: a placeholder is what puts "page 1"
    #: under a Markdown file.
    page_num: int | None
    #: The character range this passage occupies, within the page for a PDF and within the
    #: whole file for a text document. It is how a text citation is underlined; in a PDF
    #: the highlight is `bboxes`, because offsets and pdf.js's text layer disagree.
    char_start: int
    char_end: int
    text: str
    bboxes: list[dict[str, float]]
    #: Ids, resolved to names client-side against the labels the caller reaches — the same
    #: rule `GET /documents` follows, and for the same reason: a name is a disclosure.
    label_ids: list[UUID]
    lexical_rank: int | None
    dense_rank: int | None
    score: float
    # The magnitudes behind the positions, for whoever is debugging a bad answer six months
    # from now. `lexical_score` is `ts_rank_cd` and `dense_score` is cosine similarity —
    # still not comparable with each other, which is why the ranks stay.
    lexical_score: float | None = None
    dense_score: float | None = None
    rerank_score: float | None = None


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
