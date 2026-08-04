"""The generation contract, in the domain layer, owned by nobody's SDK.

This module is the boundary. Everything above it — `AnswerService`, the router, the
citation binder — speaks only the types defined here. Everything below it, in
`features/generation/adapters/`, is free to know about one vendor's JSON and is the only
place allowed to.

The rule is one sentence: **no vendor object crosses this line.** Not a response wrapper,
not a usage record, not an enum of finish reasons. The moment one does, swapping the model
stops being a configuration change and becomes a refactor, which is precisely the position
an on-premise product sold for a decade cannot afford to be in.

It lives in `common/` rather than in the feature for the same reason `exceptions.py` does:
a contract that adapters and application code both depend on cannot live inside either.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import UUID

from app.common.exceptions import ZenithError


@dataclass(frozen=True, slots=True)
class GenerationResponse:
    """What any provider returns, reduced to what this system uses.

    Deliberately narrower than every vendor's response. Tools, JSON mode, logprobs, cached
    token counts — each is something one provider has and another does not, and a field
    here for any of them would be a field that is `None` on half the installations and load-
    bearing on the other half.

    `model` is what the provider says it ran, not what was asked for: it lands in
    `queries.model_used`, and a gateway silently substituting a model is exactly the thing
    that column exists to catch.
    """

    text: str
    model: str


@dataclass(frozen=True, slots=True)
class ChunkCitation:
    """One passage the answer leaned on, resolved to something clickable.

    A citation is not the string "page 34" (mvp.md 2.9): clicking it opens the PDF on that
    page with the chunk highlighted, which is why the boxes travel with it.

    `marker` is the number as it appears in the answer text. It is an index into the
    shortlist that was sent to the model and nothing more — the model never sees a chunk id,
    so it cannot name a passage it was not shown.
    """

    marker: int
    chunk_id: UUID
    document_id: UUID
    filename: str
    page_num: int
    text: str
    bboxes: list[dict[str, float]] = field(default_factory=list[dict[str, float]])


class GenerationUnavailableError(ZenithError):
    """No model is configured, or the configured one could not be reached.

    A `ZenithError` — unlike `RerankerUnavailable`, which degrades silently into the fused
    order. There is no half an answer: without a model there is nothing to return but the
    passages, and calling that an answer would be the fabrication this feature exists to
    prevent.

    Explicitly *not* an abstention. Abstention means the corpus does not answer the
    question; saying that when nobody configured a model would be a lie about the documents.
    """

    status_code = 503
    code = "generation_unavailable"


class BaseLLMProvider(ABC):
    """Two strings in, a `GenerationResponse` out. That is the entire interface.

    An ABC rather than a `Protocol`, deliberately. A Protocol is checked at the call site,
    so a new adapter that drifts from the contract fails wherever it happens to be used —
    or does not fail at all, if the drift is a widened return type. An ABC fails at
    construction, in the adapter's own tests, which is where the person writing it is
    looking.

    Every adapter must also translate its failures into `GenerationUnavailableError`. An
    `httpx.ConnectError` reaching the router would be a vendor detail crossing the boundary
    just as surely as a response object would, and it would arrive as a 500.
    """

    #: How the adapter is named in `ZENITH_LLM_PROVIDER`. Every subclass sets it; the
    #: registry refuses to build one that has not.
    name: str = ""

    @abstractmethod
    async def complete(self, system: str, user: str) -> GenerationResponse:
        """Answer the prompt, or raise `GenerationUnavailableError`."""

    async def stream(self, system: str, user: str) -> AsyncIterator[str]:
        """Yield the answer in pieces, for `POST /query/stream`.

        Concrete rather than abstract, and defaulting to one chunk containing the whole
        answer. A provider that cannot stream is not broken — a corporate gateway that
        buffers, an adapter written before this existed — and forcing every implementation
        to reimplement `complete` in terms of a generator would make the interface harder
        to satisfy for no gain. The endpoint still works against such a provider; it simply
        delivers one large token.

        Not part of the `Connector`-shaped contract F8 defined for that reason: adding a
        method with a working default cannot break an existing adapter.
        """
        yield (await self.complete(system, user)).text
