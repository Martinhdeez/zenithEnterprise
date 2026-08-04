"""The adapter, and the failures a customer's own infrastructure will produce."""

import httpx
import pytest

from app.features.generation.adapters.openai_compatible import TEMPERATURE, OpenAiCompatible
from app.features.generation.connector import GenerationUnavailableError


def connector(handler: object, api_key: str | None = None) -> OpenAiCompatible:
    return OpenAiCompatible(
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

    completion = await connector(handler).complete("system rules", "the passages")

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

    await connector(handler).complete("s", "u")
    await connector(handler, api_key="sk-secret").complete("s", "u")

    assert seen == [None, "Bearer sk-secret"]


async def test_an_unreachable_model_is_a_domain_error_not_a_transport_one() -> None:
    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(GenerationUnavailableError, match="could not be reached"):
        await connector(dead).complete("s", "u")


async def test_the_providers_error_body_is_not_forwarded_to_the_user() -> None:
    """It is written by a system the customer configured, it can echo the API key back, and
    it reaches an end user who asked a question about a contract."""

    def rejects(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid key sk-secret-value"}})

    with pytest.raises(GenerationUnavailableError) as raised:
        await connector(rejects).complete("s", "u")

    assert "sk-secret-value" not in raised.value.message
    assert "401" in raised.value.message


async def test_a_200_with_the_wrong_shape_says_what_is_wrong() -> None:
    """The normal failure when someone points this at a URL that is nearly right — a
    gateway's index page, a health endpoint. "KeyError: choices" would not help them."""

    def wrong_shape(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    with pytest.raises(GenerationUnavailableError, match="OpenAI-compatible"):
        await connector(wrong_shape).complete("s", "u")
