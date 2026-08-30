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
import re
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

# What to say when the provider rejected the request and said nothing a person can use.
# Still good advice — for a 401 with an empty body, or a 404 from a gateway that answers in
# HTML, these really are the three things to check. It is the fallback and not the default:
# said when the provider is silent, and *replaced* when it is not, because sending somebody
# to inspect an endpoint, a model name and a key that are all correct costs half an hour and
# ends with them no closer.
ADVICE = "Check the endpoint, model name and key configured for this tenant."

# How much of the provider's own sentence survives. `error.message` is written for a human
# and is short by nature: Google's depleted-credit sentence is 140 characters. A body that
# puts a stack trace, a rendered page or an echoed request where the sentence goes is not
# writing to a person, and this ceiling is what stops one of those from becoming the error
# message a user reads.
MAX_PROVIDER_MESSAGE = 300

# Where the sentence lives, most specific first. `{"error": {"message": ...}}` is OpenAI's
# shape and Google's; `{"error": "..."}` is Ollama's and vLLM's; `detail` is what a FastAPI
# gateway in front of either produces. Anything else is a body with no sentence in it.
_MESSAGE_PATHS: tuple[tuple[str, ...], ...] = (
    ("error", "message"),
    ("error",),
    ("message",),
    ("detail",),
)

REDACTED = "[redacted]"

# Shapes a credential takes when a system echoes back the request it could not serve.
#
# Every one of them errs towards redacting too much: "Bearer token expired" loses two words
# it did not have to. That trade is deliberate and it is not close — an over-redacted error
# message is a slightly worse error message, and an under-redacted one puts a tenant's key
# in an HTTP response and a log file.
_SECRETS = (
    # An `Authorization` header quoted back, which is how a proxy reports what it forwarded.
    re.compile(r"(?i)\bbearer\s+\S+"),
    # A key in a query string. Google's own REST endpoint takes one that way, so an echoed
    # URL carries it in plain sight with no header to strip.
    re.compile(r"(?i)\b(?:api[-_]?key|key|access[-_]?token)=[^\s&\"']+"),
    # The two prefixes that announce themselves: OpenAI's and Google's.
    re.compile(r"\bsk-\S+"),
    re.compile(r"\bAIza\S+"),
)


def scrubbed(value: str, api_key: str | None = None) -> str:
    """Anything that could be a credential, replaced — the tenant's own key first.

    Two layers, because they fail differently. The **exact key** is the one match that
    cannot be wrong: this adapter is holding the secret the request was signed with, so a
    body echoing it back is caught whatever shape it took, including one no pattern
    anticipates. The **patterns** catch what the exact key cannot — a gateway echoing *its*
    upstream key rather than ours, or a key already truncated by the provider.

    Applied before the message is shortened, never after. Cutting a body at 300 characters
    first can split a key in two, and half a key that no pattern matches any more is still
    half a key in a support ticket.
    """
    # A key short enough for this to be a coincidence is a key that would redact ordinary
    # words out of the sentence. Nothing this adapter authenticates with is that short.
    if api_key and len(api_key) >= 8:
        value = value.replace(api_key, REDACTED)
    for pattern in _SECRETS:
        value = pattern.sub(REDACTED, value)
    return value


def said_by(body: str) -> str | None:
    """The provider's own sentence, or `None` when the body does not carry one.

    Only the sentence — never the body. The body is a JSON object with a status enum, a
    code, sometimes a request id and, on a gateway, whatever it was handed; `error.message`
    is the one member written *to be read*, and it is the only one lifted. Forwarding the
    rest would put a vendor's internal vocabulary in front of a person asking a question
    about a contract, and would widen the leak surface for no gain.

    A body that is not JSON returns `None` rather than its first 300 characters. An HTML
    error page from a load balancer has no sentence to lift, and lifting its markup would
    replace the advice with noise.
    """
    try:
        payload: object = json.loads(body)
    except ValueError:
        return None

    for path in _MESSAGE_PATHS:
        value: object = payload
        for key in path:
            value = _field(value, key)
        if isinstance(value, str) and value.strip():
            # Collapsed rather than kept verbatim: a message with newlines in it becomes one
            # line in a problem document and three in a log, and neither is what was written.
            return " ".join(value.split())
    return None


def rejection(status: int, body: str, api_key: str | None = None) -> str:
    """What a person is told when the provider refused the request.

    **The status stays, whatever else happens.** `429` names the class of problem before a
    word of prose is read, it is the one token that means the same thing across every
    provider, and it is what a runbook and a log filter key on. It costs three characters.

    But `429` alone is a guess, and this codebase made the wrong one: Google returns it for
    a depleted prepayment balance as well as for too many requests, so the status cannot
    tell those apart and the sentence that guessed sent an operator to check an endpoint, a
    model name and a key that were all correct. The provider had already said which it was —
    *"Your prepayment credits are depleted"* — on the wire, in the log, and thrown away one
    layer before the person who had to act on it.
    """
    said = said_by(body)
    if said is None:
        return f"the language model returned {status}. {ADVICE}"
    return f"the language model returned {status}: {_shortened(scrubbed(said, api_key))}"


def _shortened(said: str) -> str:
    if len(said) <= MAX_PROVIDER_MESSAGE:
        return said
    return said[: MAX_PROVIDER_MESSAGE - 1].rstrip() + "…"


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

    def _rejected(self, status: int, body: str) -> GenerationUnavailableError:
        """One place both refusal paths go through, so they cannot say different things.

        `complete` and `stream` are the only two calls this adapter makes and they used to
        build their own sentence — the buffered one with the advice, the streamed one with
        the status and a full stop. The same provider, the same failure, two different
        accounts of it depending on which endpoint the user happened to be on.

        **The log line is scrubbed too, and that is not belt-and-braces.** It carries the
        whole body rather than the one sentence, so it is the *more* exposed of the two
        surfaces: a message may be read by one operator, a log is shipped, indexed and kept.
        It was logging `response.text[:500]` unredacted, and the paragraph directly above it
        explaining that a body can echo the API key back is what makes that hard to defend.
        """
        log.warning("generation_rejected", status=status, body=scrubbed(body, self.api_key)[:500])
        return GenerationUnavailableError(rejection(status, body, self.api_key))

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
            raise self._rejected(response.status_code, response.text)

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
                    # Read explicitly. Nothing has touched the body yet — that is what
                    # `client.stream` is for — so `response.text` is empty until it is, and
                    # this path said `the language model returned 429.` and stopped. It is
                    # the path the chat uses, so it was the surface with the least to say
                    # about the failure people met most often.
                    await response.aread()
                    raise self._rejected(response.status_code, response.text)
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
