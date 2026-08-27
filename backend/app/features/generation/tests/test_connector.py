"""The adapter, and the failures a customer's own infrastructure will produce."""

import httpx
import pytest

from app.common.llm import GenerationUnavailableError
from app.features.generation.adapters.openai_compatible import TEMPERATURE, OpenAIProvider


def provider(handler: object, api_key: str | None = None) -> OpenAIProvider:
    return OpenAIProvider(
        endpoint_url="http://model/v1",
        model="llama3.1:8b",
        api_key=api_key,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


def replies(text: str) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    return handler


async def test_it_speaks_the_shape_every_local_server_already_speaks() -> None:
    """Ollama, vLLM, llama.cpp and OpenAI all expose this. That is the whole reason for
    targeting it rather than any vendor's SDK."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(httpx.Response(200, content=request.content).json())
        captured["path"] = request.url.path
        return httpx.Response(200, json={"choices": [{"message": {"content": "answer [1]"}}]})

    completion = await provider(handler).complete("system rules", "the passages")

    assert captured["path"] == "/v1/chat/completions"
    assert captured["messages"] == [
        {"role": "system", "content": "system rules"},
        {"role": "user", "content": "the passages"},
    ]
    assert captured["temperature"] == TEMPERATURE, "grounded answers, not creative ones"
    assert completion.text == "answer [1]"
    assert completion.model == "llama3.1:8b"


async def test_the_api_key_is_sent_only_when_there_is_one() -> None:
    """A local Ollama has no key and rejects nothing; sending `Bearer None` to one that
    does check would fail for a reason nobody could read."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"choices": [{"message": {"content": "x [1]"}}]})

    await provider(handler).complete("s", "u")
    await provider(handler, api_key="sk-secret").complete("s", "u")

    assert seen == [None, "Bearer sk-secret"]


async def test_an_unreachable_model_is_a_domain_error_not_a_transport_one() -> None:
    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(GenerationUnavailableError, match="could not be reached"):
        await provider(dead).complete("s", "u")


async def test_the_providers_error_body_is_not_forwarded_to_the_user() -> None:
    """It is written by a system the customer configured, it can echo the API key back, and
    it reaches an end user who asked a question about a contract."""

    def rejects(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid key sk-secret-value"}})

    with pytest.raises(GenerationUnavailableError) as raised:
        await provider(rejects).complete("s", "u")

    assert "sk-secret-value" not in raised.value.message
    assert "401" in raised.value.message


async def test_a_200_with_the_wrong_shape_says_what_is_wrong() -> None:
    """The normal failure when someone points this at a URL that is nearly right — a
    gateway's index page, a health endpoint. "KeyError: choices" would not help them."""

    def wrong_shape(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    with pytest.raises(GenerationUnavailableError, match="OpenAI-compatible"):
        await provider(wrong_shape).complete("s", "u")


def test_a_streamed_usage_chunk_is_read_rather_than_skipped() -> None:
    """The chunk that made the cost dashboard useless.

    With `include_usage`, the final SSE chunk has an *empty* `choices` and a populated
    `usage`. A decoder that only looked for text threw it away, so every streamed answer —
    which is every answer the chat produces — recorded NULL tokens and the dashboard had
    nothing to show for the path people actually use.
    """
    from app.features.generation.adapters.openai_compatible import (
        decoded,
        delta_of,
        usage_of,
    )

    final = decoded('data: {"choices":[],"usage":{"prompt_tokens":812,"completion_tokens":97}}')

    assert final is not None
    assert delta_of(final) is None, "it carries no text, which is why it was being dropped"
    assert usage_of(final) == (812, 97)


def test_an_ordinary_chunk_still_yields_its_text() -> None:
    from app.features.generation.adapters.openai_compatible import (
        decoded,
        delta_of,
    )

    chunk = decoded('data: {"choices":[{"delta":{"content":"Fines"}}]}')

    assert chunk is not None
    assert delta_of(chunk) == "Fines"


def test_a_provider_that_reports_nothing_gives_none_rather_than_zero() -> None:
    """An unknown cost and a zero cost are different answers, and only one is ever true.
    A local binding reports no usage at all."""
    from app.features.generation.adapters.openai_compatible import (
        usage_of,
    )

    assert usage_of({"choices": []}) == (None, None)
    # A proxy is free to send it as a string; anything that is not an integer is "not
    # reported" rather than coerced into a number somebody will budget against.
    assert usage_of({"usage": {"prompt_tokens": "812"}}) == (None, None)
