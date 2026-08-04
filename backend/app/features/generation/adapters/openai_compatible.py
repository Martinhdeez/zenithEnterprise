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
        return GenerationResponse(
            text=_first_message(payload),
            # What the server says it ran, falling back to what we asked for. A gateway
            # substituting a model silently is exactly what `queries.model_used` is for.
            model=_model_of(payload) or self.model,
        )


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
