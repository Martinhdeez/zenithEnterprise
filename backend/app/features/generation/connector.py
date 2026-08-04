"""What "pluggable" actually means, written as a type.

The interface is deliberately narrower than any vendor's API. Tools, JSON mode, logprobs,
prefill — every one of those is something one provider has and another does not, and the
moment this protocol carries one, an installation pointed at a different endpoint stops
working for reasons the customer cannot see. Two strings in, text and a model name out.

The folder is `generation/` with `adapters/openai_compatible.py` inside it rather than
`openai/`, for the same reason: a package named after a vendor starts lying the day someone
plugs in Bedrock.
"""

from dataclasses import dataclass
from typing import Protocol

from app.common.exceptions import ZenithError


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    model: str


class Connector(Protocol):
    async def complete(self, system: str, user: str) -> Completion: ...


class GenerationUnavailableError(ZenithError):
    """No model is configured, or the configured one could not be reached.

    A `ZenithError` — unlike `RerankerUnavailable`, which degrades silently into the fused
    order. There is no half an answer: without a model there is nothing to return but the
    passages, and pretending that is an answer would be the fabrication this feature exists
    to prevent.

    Explicitly *not* an abstention. Abstention means the corpus does not answer the
    question; saying that when nobody configured a model would be a lie about the documents.
    """

    status_code = 503
    code = "generation_unavailable"
