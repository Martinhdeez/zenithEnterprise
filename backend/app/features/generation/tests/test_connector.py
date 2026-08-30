"""The adapter, and the failures a customer's own infrastructure will produce."""

import json

import httpx
import pytest
from structlog.testing import capture_logs

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


# --- What the provider said ------------------------------------------------------------
#
# The two tests below are a pair and neither means much alone. The whole defect was one
# case impersonating the other: a 429 that meant "your prepayment balance is empty" was
# reported with the sentence written for a 429 that means "you are misconfigured", and the
# operator spent half an hour checking an endpoint, a model name and a key that were fine.


async def test_what_the_provider_said_is_what_the_person_is_told() -> None:
    """The live failure, verbatim from the log of the installation that produced it.

    Google returns `429` for a depleted prepayment balance as well as for too many
    requests, so the status alone cannot tell them apart — and this codebase guessed the
    wrong one. The sentence naming which it was had been on the wire the whole time.
    """

    def depleted(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "error": {
                    "code": 429,
                    "message": (
                        "Your prepayment credits are depleted. Please go to AI Studio at "
                        "https://ai.studio/projects to manage your project and billing."
                    ),
                    "status": "RESOURCE_EXHAUSTED",
                }
            },
        )

    with pytest.raises(GenerationUnavailableError) as raised:
        await provider(depleted).complete("s", "u")

    assert "prepayment credits are depleted" in raised.value.message
    # The status earns its place beside the words: it names the class of problem before a
    # sentence is read, and it means the same thing whichever provider is configured.
    assert "429" in raised.value.message
    # And the advice that sent people to check three correct things is gone, because the
    # provider said which one thing was actually wrong.
    assert "Check the endpoint" not in raised.value.message
    # The sentence, not the body. `RESOURCE_EXHAUSTED` is a vendor's internal vocabulary and
    # nobody asking a question about a contract needs to read it.
    assert "RESOURCE_EXHAUSTED" not in raised.value.message


async def test_a_body_with_nothing_to_say_still_gets_the_advice() -> None:
    """The fallback, and it is still the right sentence.

    An empty body, a timeout, a gateway answering 502 with an HTML page — none of them say
    which of the three things is wrong, and for those the three things really are what to
    check. Keeping this as the *fallback* rather than the default is the entire fix.
    """
    pages = "<html><head><title>502 Bad Gateway</title></head><body>nginx</body></html>"

    def useless(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=pages.encode(), headers={"content-type": "text/html"})

    with pytest.raises(GenerationUnavailableError) as raised:
        await provider(useless).complete("s", "u")

    assert raised.value.message == (
        "the language model returned 502. Check the endpoint, model name and key "
        "configured for this tenant."
    )
    # Not the first 300 characters of markup with the advice removed. There is no sentence
    # in a rendered page, and half of one is worse than the generic advice it replaced.
    assert "nginx" not in raised.value.message


async def test_no_key_reaches_the_message_or_the_log() -> None:
    """The body is written by a system the customer configured, and it can echo the request.

    A request carries the key — in the `Authorization` header, and for Google's REST
    endpoint in the URL — so a gateway that reports "could not forward: <the request>" puts
    the tenant's secret in a body this code is about to read. Two layers catch it: the exact
    key, which this adapter is holding and therefore cannot mistake, and the shapes a
    credential takes when it belongs to somebody else's upstream.

    Asserted on the log line as well as the message. The log carries the whole body rather
    than the one sentence, so it is the more exposed of the two: a message is read once, a
    log is shipped, indexed and kept.

    `structlog.testing.capture_logs` rather than pytest's `caplog`, because this codebase
    logs through structlog and nothing routes it to the standard library. Written with
    `caplog` first, this test passed against an *unscrubbed* log line — `caplog.text` was
    the empty string, so every `not in` held for the wrong reason. A test that cannot fail
    is the one shape of test worth nothing at all.
    """
    key = "AIzaSyD-ThisIsTheTenantsRealKey-000"

    def echoes(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "message": (
                        f"could not forward request: GET /v1beta/models?key={key} "
                        f"with header Authorization: Bearer {key} "
                        f"(upstream credential sk-liveUpstreamKeyABCDEF was also rejected)"
                    )
                }
            },
        )

    with capture_logs() as logged:
        with pytest.raises(GenerationUnavailableError) as raised:
            await provider(echoes, api_key=key).complete("s", "u")

    # The line really was written, so the assertions below are about a scrubbed log rather
    # than about an empty one.
    written = [entry for entry in logged if entry["event"] == "generation_rejected"]
    assert len(written) == 1
    line = str(written[0])
    # The body reached the log scrubbed rather than emptied — otherwise the `not in`
    # assertions below would hold against a log line with nothing in it.
    assert "could not forward request" in line

    assert key not in raised.value.message
    assert key not in line
    # Not only ours. A gateway echoing the key it uses upstream is leaking somebody else's
    # secret through our response, and the exact-key layer cannot see that one at all.
    assert "sk-liveUpstreamKeyABCDEF" not in raised.value.message
    assert "sk-liveUpstreamKeyABCDEF" not in line
    # Redacted, not dropped: the person still needs to know the request was rejected and
    # that the gateway was quoting it back.
    assert "could not forward request" in raised.value.message


async def test_the_streamed_path_says_as_much_as_the_buffered_one() -> None:
    """The path the chat uses, and the one that said least.

    `client.stream` has not touched the body when the status arrives, so this branch read
    nothing and reported `the language model returned 429.` — the same failure, reported
    worse, on the endpoint people meet it on most.
    """

    def depleted(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "Your prepayment credits are depleted."}})

    with pytest.raises(GenerationUnavailableError) as raised:
        async for _ in provider(depleted).stream("s", "u"):
            pass

    assert "prepayment credits are depleted" in raised.value.message


def test_a_provider_sentence_is_taken_from_the_shapes_providers_actually_use() -> None:
    """`error.message` is OpenAI's and Google's; the rest are what the others do instead."""
    from app.features.generation.adapters.openai_compatible import said_by

    assert said_by('{"error": {"message": "quota exceeded"}}') == "quota exceeded"
    assert said_by('{"error": "model not found"}') == "model not found"
    assert said_by('{"message": "unauthorised"}') == "unauthorised"
    assert said_by('{"detail": "Not Found"}') == "Not Found"
    # Nothing written to be read. `{"error": {"code": 429}}` is a status, not a sentence.
    assert said_by('{"error": {"code": 429}}') is None
    assert said_by('{"error": {"message": "   "}}') is None
    assert said_by("") is None
    assert said_by("<html>503</html>") is None


def test_a_long_body_cannot_become_the_error_message() -> None:
    """A provider that puts a stack trace in `error.message` is not writing to a person."""
    from app.features.generation.adapters.openai_compatible import (
        MAX_PROVIDER_MESSAGE,
        rejection,
    )

    trace = "Traceback: " + "at some.internal.Frame(Frame.java:41) " * 200
    message = rejection(500, json.dumps({"error": {"message": trace}}))

    assert len(message) < MAX_PROVIDER_MESSAGE + 60
    assert message.endswith("…")


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
