"""Google's own `generateContent`, which is not the OpenAI shape and cannot be made into it.

`openai_compatible` already reaches Gemini through the compatibility layer at
`/v1beta/openai`, so a second adapter needs a reason. The reason is a credential. Measured
against Google directly, with the key the customer actually holds:

| endpoint | auth | result |
|---|---|---|
| `/v1beta/models` | `Authorization: Bearer` | 401, "Expected OAuth 2 access token…" |
| `/v1beta/models` | `x-goog-api-key` | 200 |
| `/v1beta/openai/chat/completions` | `Authorization: Bearer` | read timeout |
| `/v1beta/openai/chat/completions` | `?key=` | 400, "Missing or invalid Authorization header" |
| `/v1beta/models/{model}:generateContent` | `x-goog-api-key` | 200, generated text |

The compatibility layer authenticates by bearer token and nothing else. Google issues more
than one kind of credential — the 53-character `AQ.Ab8…` this was written for is not the
39-character `AIza…` shape — and the layer rejects the one this customer has while the
native API accepts it. That is not a configuration problem anybody can fix from the admin
screen, which is what makes it an adapter rather than a support answer.

**The differences from the OpenAI shape are not cosmetic**, and each is a place a
translation layer would have to make something up:

- The model is in the *path*, not the body, and the verb is glued to it with a colon.
- The system prompt is `system_instruction`, a sibling of `contents` rather than the first
  message in it.
- The answer is `candidates[].content.parts[].text` — a *list* of parts, which may include
  the model's own reasoning marked `thought`, and which is present-but-empty when the
  answer was truncated or refused.
- Token counts are `usageMetadata`, and the thinking models bill for tokens that appear in
  neither the prompt nor the answer.

What it does *not* differ on is what this codebase needs: a system prompt lands, streaming
is real SSE, and the counts are reported. Nothing `AnswerService` relies on is missing.

The error-reading helpers are imported from `openai_compatible` rather than copied. They
are not OpenAI's — `said_by` was written *for Google's* `error.message`, list wrapper and
all — and two copies of the rule that decides what a person is told when a provider refuses
is exactly the drift that put the generic advice in front of a depleted-credit 429. They
belong in a module named for the job rather than for a vendor; moving them is a change to a
file this branch does not own.
"""

from collections.abc import AsyncIterator
from typing import cast

import httpx
import structlog

from app.common.llm import BaseLLMProvider, GenerationResponse, GenerationUnavailableError
from app.features.generation.adapters.openai_compatible import (
    TEMPERATURE,
    TIMEOUT,
    decoded,
    rejection,
    scrubbed,
)

log = structlog.get_logger()

# Google names its models `models/gemini-3.6-flash` in `ListModels` and takes
# `gemini-3.6-flash` in the path. An administrator copying a name out of the model list is
# doing the obvious thing, and the reward for it is a 404 that says the model does not
# exist. The prefix is stripped rather than rejected: there is exactly one correct reading
# of `models/models/gemini-3.6-flash`, and it is not a 404.
_MODEL_PREFIX = "models/"

# What the key travels in. **Not the query string**, and this is the one deliberate
# departure from how Google's own quick-start writes it.
#
# `?key=` works — it was the first thing measured. But a key in a URL is a key in every
# access log, every proxy log, every exception that renders a request, and every `curl`
# somebody pastes into a ticket, and none of those are places this code controls. The
# header form was then measured against the same live endpoint with the same customer
# credential and returned 200, so the entire class of leak is removable rather than
# guardable. `scrubbed` still runs on every message and every log line, because a
# *gateway* in front of this, or an administrator's own pasted URL, can still put one
# there — but nothing this adapter constructs ever carries a secret in a URL.
API_KEY_HEADER = "x-goog-api-key"


class GeminiProvider(BaseLLMProvider):
    name = "gemini"

    #: Token counts from the last streamed call, or `(None, None)`. Same contract, and same
    #: single-use-per-request caveat, as `OpenAIProvider.last_usage`.
    last_usage: tuple[int | None, int | None] = (None, None)

    def __init__(
        self,
        endpoint_url: str,
        model: str,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        #: The base of the native API — `https://generativelanguage.googleapis.com/v1beta`,
        #: with no `/openai` on the end. A row still holding the compatibility layer's URL
        #: builds `/v1beta/openai/models/…:generateContent`, which Google answers 404 with
        #: an empty body; that lands on the generic advice, which for a wrong endpoint is
        #: the correct sentence.
        self.endpoint_url = endpoint_url.rstrip("/")
        self.model = model.removeprefix(_MODEL_PREFIX)
        self.api_key = api_key
        self.transport = transport

    def _url(self, verb: str) -> str:
        return f"{self.endpoint_url}/models/{self.model}:{verb}"

    def _headers(self) -> dict[str, str]:
        return {API_KEY_HEADER: self.api_key} if self.api_key else {}

    def _body(self, system: str, user: str) -> dict[str, object]:
        return {
            # A sibling of `contents`, not the first entry in it. Sent under the REST
            # spelling, which the endpoint accepts alongside the camelCase one.
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": TEMPERATURE},
        }

    def _unreachable(self, exc: httpx.HTTPError) -> GenerationUnavailableError:
        """The endpoint and the model, never the URL that was actually requested.

        `self._url(...)` is safe today because the key rides in a header, but this message
        is the one place a future change to that would leak silently — it is built from
        parts rather than from the request for that reason.
        """
        return GenerationUnavailableError(
            f"the language model at {self.endpoint_url} could not be reached "
            f"({type(exc).__name__})."
        )

    def _rejected(self, status: int, body: str) -> GenerationUnavailableError:
        """The same account of a refusal the OpenAI adapter gives, scrubbed the same way.

        Both the message and the log line go through `scrubbed`. The log is the more
        exposed of the two — it carries the whole body rather than the one sentence, and it
        is shipped, indexed and kept — and a Google error body quotes the request it could
        not serve, which for this API has historically meant a URL with `?key=` in it.
        """
        log.warning("generation_rejected", status=status, body=scrubbed(body, self.api_key)[:500])
        return GenerationUnavailableError(rejection(status, body, self.api_key))

    async def complete(self, system: str, user: str) -> GenerationResponse:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self.transport) as client:
                response = await client.post(
                    self._url("generateContent"),
                    headers=self._headers(),
                    json=self._body(system, user),
                )
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc

        if response.status_code >= 400:
            raise self._rejected(response.status_code, response.text)

        payload: object = response.json()
        prompt_tokens, completion_tokens = usage_of(payload)
        return GenerationResponse(
            text=text_of(payload),
            model=model_of(payload) or self.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    async def stream(self, system: str, user: str) -> AsyncIterator[str]:
        """`streamGenerateContent` with `alt=sse`, which is the same `data:` framing.

        Without `alt=sse` the endpoint streams a *JSON array* instead — one document
        arriving in pieces, which cannot be read a line at a time. The query parameter is
        therefore load-bearing rather than a preference.

        Every chunk carries the whole `usageMetadata` so far, so `last_usage` is overwritten
        rather than accumulated and the final write is the authoritative one.

        **A stream that produced no text raises rather than ending quietly.** The reason is
        `text_of`'s: silence reaches the citation binder as an answer with no citation, is
        discarded, and is reported to the user as an abstention — the product saying the
        corpus does not answer the question when what actually happened is that the model
        refused or was cut off. Once any text has been yielded the failure is no longer
        available to raise, and a truncated answer is the honest outcome.
        """
        yielded = False
        try:
            async with (
                httpx.AsyncClient(timeout=TIMEOUT, transport=self.transport) as client,
                client.stream(
                    "POST",
                    self._url("streamGenerateContent"),
                    params={"alt": "sse"},
                    headers=self._headers(),
                    json=self._body(system, user),
                ) as response,
            ):
                if response.status_code >= 400:
                    # Nothing has touched the body yet — that is what `client.stream` is
                    # for — so `response.text` is empty until it is read.
                    await response.aread()
                    raise self._rejected(response.status_code, response.text)
                last: object = None
                async for line in response.aiter_lines():
                    payload = decoded(line)
                    if payload is None:
                        continue
                    last = payload
                    reported = usage_of(payload)
                    if reported != (None, None):
                        self.last_usage = reported
                    content = delta_of(payload)
                    if content:
                        yielded = True
                        yield content
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc

        if not yielded:
            raise GenerationUnavailableError(_silence(last))


# Public rather than underscored, and the three below with it. They are the `generateContent`
# response contract this adapter implements — which member carries the answer, which carries
# the cost, what a refusal looks like — and that is worth naming and testing directly rather
# than reaching into through a live call.
def text_of(payload: object) -> str:
    """The answer, or an error — never the empty string.

    This is the function with the wrong answer that looks fine. `generateContent` returns
    200 with a well-formed body in every one of these cases:

    - the prompt was blocked before the model saw it (`promptFeedback.blockReason`, and no
      `candidates` member at all);
    - the candidate was blocked after generation (`finishReason: SAFETY`, and no `content`);
    - the answer was truncated at the first token (`finishReason: MAX_TOKENS`, and
      `parts: [{"text": ""}]` — measured, not assumed);
    - every part is the model's own reasoning, marked `thought`.

    Returning `""` for any of them puts an empty answer into `AnswerService`, where it
    carries no citation, is discarded by the binder, and is reported to the user as an
    abstention. Abstention means *the corpus does not answer this question*. Saying that
    when the model was blocked or cut off is a lie about the customer's documents, and it
    is the one lie this feature exists to prevent — `MockProvider` cites its first passage
    for the same reason. So each of them raises, naming the reason the provider gave.
    """
    candidates = _field(payload, "candidates")
    if not isinstance(candidates, list) or not candidates:
        blocked = _field(_field(payload, "promptFeedback"), "blockReason")
        if isinstance(blocked, str) and blocked:
            raise GenerationUnavailableError(
                f"the model returned no answer: the prompt was blocked ({blocked})."
            )
        raise GenerationUnavailableError(
            "the endpoint answered without any candidates. It may not be a "
            "generateContent URL for this model."
        )

    candidate = cast(list[object], candidates)[0]
    answer = "".join(_texts(candidate))
    if not answer.strip():
        reason = _field(candidate, "finishReason")
        named = reason if isinstance(reason, str) and reason else "no reason given"
        raise GenerationUnavailableError(
            f"the model returned an empty answer ({named}). Nothing was generated, so "
            f"there is no answer to cite."
        )
    return answer


def delta_of(payload: object) -> str | None:
    """The text one streamed chunk carries, or `None` for one that carries none.

    `None` rather than an exception, unlike `text_of`. A stream's chunks legitimately carry
    no text — the last one is usually a `thoughtSignature` with `"text": ""` beside it — and
    the judgement about silence belongs to the stream as a whole, which is where `stream`
    makes it.
    """
    candidates = _field(payload, "candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    text = "".join(_texts(cast(list[object], candidates)[0]))
    return text or None


def _texts(candidate: object) -> list[str]:
    """Every part of one candidate that is an answer, in order.

    A list, because `parts` is one: a single response can arrive split across several, and
    reading only the first would silently truncate the answer at whatever boundary the model
    happened to use.

    **Parts marked `thought` are dropped.** They are the model's reasoning, and the thinking
    models emit them whenever `includeThoughts` is on. This adapter does not ask for them,
    so today the filter removes nothing — which is the argument for it rather than against
    it: the day a generation setting turns them on, the alternative is the model's private
    deliberation appearing to the customer as the answer to their question, cited to their
    own documents.
    """
    parts = _field(_field(candidate, "content"), "parts")
    if not isinstance(parts, list):
        return []
    found: list[str] = []
    for part in cast(list[object], parts):
        if _field(part, "thought") is True:
            continue
        text = _field(part, "text")
        if isinstance(text, str) and text:
            found.append(text)
    return found


def model_of(payload: object) -> str | None:
    """`modelVersion` — what Google says it ran, which is not always what was asked for.

    `gemini-flash-latest` is an alias and resolves to whatever is current; the whole point
    of `queries.model_used` is that the answer records which model actually produced it.
    """
    model = _field(payload, "modelVersion")
    return model if isinstance(model, str) else None


def usage_of(payload: object) -> tuple[int | None, int | None]:
    """`usageMetadata`, read as prompt and completion counts.

    **`thoughtsTokenCount` is added to the completion count, and that is a decision.** The
    thinking models spend tokens that appear in neither the prompt nor the answer — a
    four-word reply measured 15 prompt, 5 candidate and 316 thought tokens — and Google
    bills them as output. Reporting 5 would put a number in the cost dashboard that is off
    by a factor of sixty, and a confidently wrong figure in a cost report is worse than no
    figure, because somebody budgets against it.

    `(None, None)` when nothing is reported, which stays a different answer from `(0, 0)`.
    Anything that is not an integer is treated as not reported: a truncated response omits
    `candidatesTokenCount` entirely, and inventing a zero for it would be the same lie in
    the other direction.
    """
    usage = _field(payload, "usageMetadata")
    if not isinstance(usage, dict):
        return None, None

    def count(key: str) -> int | None:
        value = cast(dict[str, object], usage).get(key)
        return value if isinstance(value, int) else None

    answered, thought = count("candidatesTokenCount"), count("thoughtsTokenCount")
    completion = None if answered is None and thought is None else (answered or 0) + (thought or 0)
    return count("promptTokenCount"), completion


def _silence(last: object) -> str:
    """What to say when a stream ended without a word of answer in it.

    Built from the last chunk seen, because that is where the reason is: a refusal arrives
    as a final chunk with a `finishReason` and no text, not as a status code.
    """
    candidates = _field(last, "candidates")
    if isinstance(candidates, list) and candidates:
        reason = _field(cast(list[object], candidates)[0], "finishReason")
        if isinstance(reason, str) and reason:
            return (
                f"the model returned an empty answer ({reason}). Nothing was generated, so "
                f"there is no answer to cite."
            )
    blocked = _field(_field(last, "promptFeedback"), "blockReason")
    if isinstance(blocked, str) and blocked:
        return f"the model returned no answer: the prompt was blocked ({blocked})."
    return "the model streamed no answer at all."


def _field(value: object, key: str) -> object:
    """One step into decoded JSON, returning `None` for anything that is not a mapping.

    A private copy of the helper next door rather than an import of it. It is four lines
    with no policy in them, and reaching into another adapter's underscored names to save
    them would couple the two modules at exactly the place neither guarantees anything.
    """
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value).get(key)
