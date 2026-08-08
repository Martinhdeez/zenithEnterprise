"""One adapter, covering everything that speaks `/v1/chat/completions`.

Which is Ollama, vLLM, llama.cpp, LM Studio, OpenAI, Together, Groq, Fireworks, and
whatever an enterprise puts behind its own gateway. The shape was standardised by adoption
rather than by a committee, and that is exactly why it is the right thing to target for an
on-premise product: it is the format a customer's existing infrastructure most likely
already exposes.

The class is `OpenAIProvider` and the module is `openai_compatible` — the distinction is
worth keeping. The protocol is OpenAI's; the servers speaking it here are mostly not
OpenAI, and the module name is what stops someone reading the tree from concluding this
codebase talks to one vendor. mvp.md 5.1 made the same call about the folder.

No vendor SDK. `httpx` is already a dependency, the request is one JSON object, and a
vendor SDK would drag in its own retry policy, its own timeout defaults and its own opinion
about environment variables — three things this codebase has decided for itself. It would
also put a vendor's response type one import away from the application layer, which is the
line `common/llm.py` exists to hold.
"""

import json
from collections.abc import AsyncIterator
from typing import cast

import httpx
import structlog

from app.common.llm import BaseLLMProvider, GenerationResponse, GenerationUnavailableError

log = structlog.get_logger()

# Generous compared with everything else in this codebase, and it has to be: an 8B model on
# CPU writing a paragraph with citations is doing far more work per request than an
# embedding. Still finite — an interactive endpoint that hangs is worse than one that fails.
TIMEOUT = 120.0

# Low, not zero. The point of this feature is that the answer is grounded in passages the
# model was handed; sampling widely is how a model starts writing plausible prose that
# drifts off them. Not zero because several servers treat 0 as "unset".
TEMPERATURE = 0.1


class OpenAIProvider(BaseLLMProvider):
    name = "openai"

    #: Token counts from the last streamed call, or `(None, None)`. See `stream`.
    last_usage: tuple[int | None, int | None] = (None, None)

    def __init__(
        self,
        endpoint_url: str,
        model: str,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.endpoint_url = endpoint_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.transport = transport

    async def complete(self, system: str, user: str) -> GenerationResponse:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self.transport) as client:
                response = await client.post(
                    f"{self.endpoint_url}/chat/completions",
                    headers=headers,
                    json={
                        "model": self.model,
                        "temperature": TEMPERATURE,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                    },
                )
        except httpx.HTTPError as exc:
            # Translated here rather than allowed to propagate. An `httpx.ConnectError`
            # reaching the router is a vendor detail crossing the boundary just as surely
            # as a response object would be, and it would arrive as a 500.
            raise GenerationUnavailableError(
                f"the language model at {self.endpoint_url} could not be reached "
                f"({type(exc).__name__})."
            ) from exc

        if response.status_code >= 400:
            # The body is not forwarded. It is written by a system the customer configured,
            # it can contain the API key echoed back, and this message reaches an end user
            # asking a question about a contract.
            log.warning(
                "generation_rejected", status=response.status_code, body=response.text[:500]
            )
            raise GenerationUnavailableError(
                f"the language model returned {response.status_code}. Check the endpoint, "
                f"model name and key configured for this tenant."
            )

        payload: object = response.json()
        prompt_tokens, completion_tokens = usage_of(payload)
        return GenerationResponse(
            text=_first_message(payload),
            # What the server says it ran, falling back to what we asked for. A gateway
            # substituting a model silently is exactly what `queries.model_used` is for.
            model=_model_of(payload) or self.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    async def stream(self, system: str, user: str) -> AsyncIterator[str]:
        """The same request with `stream: true`, decoded from SSE.

        Sets `last_usage` as a side effect, read by the caller once the stream is done.
        Stateful and therefore worth being explicit about: a provider is built per request
        by `AnswerService._resolve`, so there is no sharing to race over — but a provider
        reused across concurrent requests would report the wrong numbers, and that is the
        constraint anybody moving this code has to keep.

        Parsed by hand rather than with a library: the response is `data: {...}` lines and a
        `data: [DONE]` sentinel, and a dependency that exists to split on a colon would be a
        dependency to audit, license and upgrade for the next decade of an on-premise
        product.

        A malformed line is skipped rather than fatal. Half a streamed answer that keeps
        flowing is worth more than an exception thrown at the client mid-sentence, and the
        `result` event at the end is what carries the authoritative text regardless.
        """
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with (
                httpx.AsyncClient(timeout=TIMEOUT, transport=self.transport) as client,
                client.stream(
                    "POST",
                    f"{self.endpoint_url}/chat/completions",
                    headers=headers,
                    json={
                        "model": self.model,
                        "temperature": TEMPERATURE,
                        "stream": True,
                        # Asked for explicitly, because a streamed response carries no
                        # usage object otherwise — which is why every streamed answer
                        # recorded NULL tokens and the cost dashboard had nothing to show
                        # for the path the chat actually uses. An endpoint that does not
                        # understand this key ignores it; one that does sends a final chunk
                        # whose `choices` is empty and whose `usage` is the whole point.
                        "stream_options": {"include_usage": True},
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                    },
                ) as response,
            ):
                if response.status_code >= 400:
                    raise GenerationUnavailableError(
                        f"the language model returned {response.status_code}."
                    )
                async for line in response.aiter_lines():
                    payload = decoded(line)
                    if payload is None:
                        continue
                    self.last_usage = (
                        usage_of(payload) if _field(payload, "usage") else (self.last_usage)
                    )
                    content = delta_of(payload)
                    if content:
                        yield content
        except httpx.HTTPError as exc:
            raise GenerationUnavailableError(
                f"the language model at {self.endpoint_url} could not be reached "
                f"({type(exc).__name__})."
            ) from exc


# Public rather than underscored, and the three below with it. They are the SSE contract
# this adapter implements — which chunk carries text, which carries the cost — and that is
# a thing worth naming and testing directly rather than reaching into.
def decoded(line: str) -> object | None:
    """One SSE line to its JSON object, or `None` for everything that is not one.

    Split from reading the delta because the final chunk of a stream with
    `include_usage` has an *empty* `choices` and a populated `usage` — so a decoder that
    returned only the text would throw away the one chunk the cost report needs.

    A malformed line is skipped rather than fatal. Half a streamed answer that keeps
    flowing is worth more than an exception thrown mid-sentence.
    """
    if not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def delta_of(decoded: object) -> str | None:
    """The text one streamed chunk carries, or `None` — including for the usage chunk."""
    choices = _field(decoded, "choices")
    if not isinstance(choices, list) or not choices:
        return None
    content = _field(_field(cast(list[object], choices)[0], "delta"), "content")
    return content if isinstance(content, str) and content else None


def _first_message(payload: object) -> str:
    """Read the one field that matters, and complain precisely when it is not there.

    A server that answers 200 with a shape nobody expected is the normal failure when
    someone points this at a URL that is *nearly* the right one — a proxy's index page, a
    gateway's health endpoint. "KeyError: choices" three frames down does not tell them
    that; this does.
    """
    choices = _field(payload, "choices")
    if not isinstance(choices, list) or not choices:
        raise GenerationUnavailableError(
            "the endpoint answered without any choices. It may not be an "
            "OpenAI-compatible /v1/chat/completions URL."
        )
    content = _field(_field(cast(list[object], choices)[0], "message"), "content")
    if not isinstance(content, str):
        raise GenerationUnavailableError("the endpoint answered with no message content.")
    return content


def _model_of(payload: object) -> str | None:
    model = _field(payload, "model")
    return model if isinstance(model, str) else None


def _field(value: object, key: str) -> object:
    """One step into decoded JSON, returning `None` for anything that is not a mapping.

    A helper rather than a chain of `isinstance` checks inline, because the shape being
    walked is whatever a customer's endpoint chose to send. Every step has to survive it
    being something else entirely.
    """
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value).get(key)


def usage_of(payload: object) -> tuple[int | None, int | None]:
    """Token counts, if this endpoint reports any.

    `(None, None)` when it does not, and that is a different answer from `(0, 0)`. A local
    binding reports nothing and a gateway may strip `usage`; recording zero for either would
    put a confident, wrong number in a cost report that somebody budgets against.

    Read defensively rather than trusted: `usage` is not in the OpenAI *streaming* response
    at all, and a proxy is free to send it as a string. Anything that is not an integer is
    treated as not reported.
    """
    usage = _field(payload, "usage")
    if not isinstance(usage, dict):
        return None, None

    def count(key: str) -> int | None:
        value = cast(dict[str, object], usage).get(key)
        return value if isinstance(value, int) else None

    return count("prompt_tokens"), count("completion_tokens")
