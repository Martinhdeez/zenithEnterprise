"""The native Gemini adapter, held to the shapes that came off the wire.

Every body in this file was either read from a live `generativelanguage.googleapis.com`
response or is the documented shape of one this account could not be made to produce. The
distinction matters here more than usual: the previous round of this work had a green suite
against a specification while the live path was broken, twice, for shapes a specification
does not show. Where a body is copied from a real response the test says so.
"""

import json

import httpx
import pytest
from structlog.testing import capture_logs

from app.common.llm import GenerationUnavailableError
from app.features.generation.adapters.gemini_native import (
    API_KEY_HEADER,
    GeminiProvider,
    usage_of,
)
from app.features.generation.adapters.openai_compatible import TEMPERATURE

BASE = "https://generativelanguage.googleapis.com/v1beta"


def provider(
    handler: object, api_key: str | None = None, model: str = "gemini-3.6-flash"
) -> GeminiProvider:
    return GeminiProvider(
        endpoint_url=BASE,
        model=model,
        api_key=api_key,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


def answers(payload: object) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handler


def sse(*chunks: object) -> object:
    body = "".join(f"data: {json.dumps(chunk)}\r\n\r\n" for chunk in chunks)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )

    return handler


# The shape of a real success, trimmed of the `thoughtSignature` blob. Read from
# `gemini-3.6-flash` on 2026-08-31: the counts are that response's, and they are the reason
# `usage_of` adds two of them together.
LIVE_SUCCESS: dict[str, object] = {
    "candidates": [
        {
            "content": {"parts": [{"text": "The sky is blue."}], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 15,
        "candidatesTokenCount": 5,
        "totalTokenCount": 336,
        "thoughtsTokenCount": 316,
    },
    "modelVersion": "gemini-3.6-flash",
}


# --- The request -----------------------------------------------------------------------


async def test_the_model_and_the_verb_are_in_the_path_and_the_prompt_is_a_sibling() -> None:
    """Neither of which the OpenAI shape does, and both of which a translation layer would
    have had to invent."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=LIVE_SUCCESS)

    completion = await provider(handler).complete("system rules", "the passages")

    assert seen["path"] == "/v1beta/models/gemini-3.6-flash:generateContent"
    body = seen["body"]
    assert isinstance(body, dict)
    # A sibling of `contents`, not the first message in it.
    assert body["system_instruction"] == {"parts": [{"text": "system rules"}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "the passages"}]}]
    assert body["generationConfig"] == {"temperature": TEMPERATURE}
    assert completion.text == "The sky is blue."


async def test_a_model_copied_out_of_the_model_list_still_works() -> None:
    """`ListModels` returns `models/gemini-3.6-flash`; the path takes `gemini-3.6-flash`.
    An administrator pasting the name they were shown should not be answered with a 404
    saying that model does not exist."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=LIVE_SUCCESS)

    await provider(handler, model="models/gemini-3.6-flash").complete("s", "u")

    assert seen == ["/v1beta/models/gemini-3.6-flash:generateContent"]


# --- The key ---------------------------------------------------------------------------
#
# The three tests below are the reason this adapter is not a copy of the other one with a
# different body. Google's quick-start puts the credential in the query string, and a key
# in a URL is a key in every access log and every rendered request between here and the
# provider. It was measured that the header form works against the live endpoint with this
# customer's credential, so the leak is removed rather than guarded — and then guarded too.


async def test_the_key_travels_in_a_header_and_never_in_the_url() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["header"] = request.headers.get(API_KEY_HEADER)
        return httpx.Response(200, json=LIVE_SUCCESS)

    key = "AQ.Ab8RN6-ThisIsTheTenantsRealCredential"
    await provider(handler, api_key=key).complete("s", "u")

    assert seen["header"] == key
    url = seen["url"]
    assert isinstance(url, str)
    assert key not in url
    assert "key=" not in url


async def test_no_key_is_sent_when_there_is_none() -> None:
    """A gateway in front of Gemini may authenticate some other way; sending the header with
    an empty value would fail for a reason nobody could read."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get(API_KEY_HEADER))
        return httpx.Response(200, json=LIVE_SUCCESS)

    await provider(handler).complete("s", "u")

    assert seen == [None]


async def test_a_url_carrying_a_key_never_reaches_a_message_or_a_log() -> None:
    """The hole this adapter had to avoid, fed the body that would open it.

    Google's own errors quote the request they could not serve, and for this API the
    documented way to make one carries the credential in the query string. So the body
    below is a URL with a key in it, echoed back by the provider — which is the exact
    shape that would put a tenant's secret into an HTTP response and a log file.

    Asserted on the log line as well as the message, and on the log line *having content*
    first. `structlog.testing.capture_logs` rather than `caplog`: nothing routes structlog
    to the standard library here, so a `caplog`-based version of this test passes against a
    completely unscrubbed log by comparing against the empty string.
    """
    # The shape of the credential this was written for — 53 characters beginning `AQ.Ab8` —
    # with invented characters. A real key in a test file is a real key in the repository,
    # and the fact that this one is only used to be redacted would not make it any less
    # published.
    key = "AQ.Ab8NotARealKeyJustTheRightShape000000000000000000"

    def echoes(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "message": (
                        f"could not forward request: POST {BASE}/models/x:generateContent"
                        f"?key={key} (upstream credential AIzaSyD-SomebodyElsesKey-00 was "
                        f"also rejected)"
                    ),
                    "status": "PERMISSION_DENIED",
                }
            },
        )

    with capture_logs() as logged, pytest.raises(GenerationUnavailableError) as raised:
        await provider(echoes, api_key=key).complete("s", "u")

    written = [entry for entry in logged if entry["event"] == "generation_rejected"]
    assert len(written) == 1
    line = str(written[0])
    # The body reached the log scrubbed rather than emptied, so the `not in` assertions
    # below are about a redacted log line rather than an absent one.
    assert "could not forward request" in line

    assert key not in raised.value.message
    assert key not in line
    # Not only the tenant's. A gateway echoing the key it uses upstream leaks somebody
    # else's secret through our response, and the exact-key layer cannot see that one.
    assert "AIzaSyD-SomebodyElsesKey-00" not in raised.value.message
    assert "AIzaSyD-SomebodyElsesKey-00" not in line
    # Redacted, not dropped: the person still needs to know the request was refused and
    # that the provider was quoting it back at them.
    assert "could not forward request" in raised.value.message


async def test_the_streamed_path_leaks_no_key_either() -> None:
    """The path the chat uses. Two request-building sites means two chances to get it
    wrong, and only one of them is exercised by the test above."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["header"] = request.headers.get(API_KEY_HEADER)
        return httpx.Response(
            200,
            content=b'data: {"candidates":[{"content":{"parts":[{"text":"hi"}]}}]}\r\n\r\n',
            headers={"content-type": "text/event-stream"},
        )

    key = "AQ.Ab8RN6-ThisIsTheTenantsRealCredential"
    async for _ in provider(handler, api_key=key).stream("s", "u"):
        pass

    url = seen["url"]
    assert isinstance(url, str)
    assert seen["header"] == key
    assert key not in url
    # `alt=sse` is load-bearing and is the only thing the query string may carry: without
    # it the endpoint streams a JSON array, which cannot be read a line at a time.
    assert url == f"{BASE}/models/gemini-3.6-flash:streamGenerateContent?alt=sse"


# --- Refusals --------------------------------------------------------------------------


async def test_what_the_provider_said_is_what_the_person_is_told() -> None:
    """Verbatim from a live 404 on this account. The sentence names the thing to fix; the
    generic advice would have sent somebody to check a key and an endpoint that are both
    correct."""

    def gone(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "error": {
                    "code": 404,
                    "message": (
                        "This model models/gemini-2.5-flash is no longer available to new "
                        "users. Please update your code to use models/gemini-3.6-flash for "
                        "the latest features and improvements."
                    ),
                    "status": "NOT_FOUND",
                }
            },
        )

    with pytest.raises(GenerationUnavailableError) as raised:
        await provider(gone).complete("s", "u")

    assert "no longer available to new users" in raised.value.message
    assert "404" in raised.value.message
    assert "Check the endpoint" not in raised.value.message
    # The sentence, not the body: `NOT_FOUND` is vendor vocabulary.
    assert "NOT_FOUND" not in raised.value.message


async def test_an_error_wrapped_in_a_list_of_one_is_read_like_a_plain_one() -> None:
    """Both shapes occur on this API and only one of them is in the documentation.

    The list wrapper is what Google answers a 429 with, and an implementation written from
    the specification reads the object, misses the list, and falls through to the generic
    advice — which is precisely the failure that was found against a live depleted account
    rather than in a test. It is asserted here for the native adapter because "the other
    adapter handles it" is a claim that stops being true the moment either module moves.
    """
    said = "Your prepayment credits are depleted."

    def listed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json=[{"error": {"code": 429, "message": said}}])

    def plain(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"code": 429, "message": said}})

    for handler in (listed, plain):
        with pytest.raises(GenerationUnavailableError) as raised:
            await provider(handler).complete("s", "u")
        assert said in raised.value.message
        assert "429" in raised.value.message
        assert "Check the endpoint" not in raised.value.message


async def test_a_body_with_nothing_to_say_still_gets_the_advice() -> None:
    """A live 404 with an *empty body*, which is what pointing this at the compatibility
    layer's URL — `/v1beta/openai` — actually produces. For a wrong endpoint the three
    things to check really are the three things to check."""

    def silent(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"")

    with pytest.raises(GenerationUnavailableError) as raised:
        await provider(silent).complete("s", "u")

    assert raised.value.message == (
        "the language model returned 404. Check the endpoint, model name and key "
        "configured for this tenant."
    )


async def test_an_unreachable_endpoint_is_a_domain_error_and_names_no_url() -> None:
    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    key = "AQ.Ab8RN6-ThisIsTheTenantsRealCredential"
    with pytest.raises(GenerationUnavailableError) as raised:
        await provider(dead, api_key=key).complete("s", "u")

    assert "could not be reached" in raised.value.message
    # The base, which is configuration, never the request that was built from it.
    assert BASE in raised.value.message
    assert key not in raised.value.message


# --- An answer that is not one -----------------------------------------------------------
#
# Each of these is a 200 with a well-formed body. Returning "" for any of them would reach
# the citation binder as an uncited answer, be discarded, and be reported to the user as an
# abstention — the product saying the corpus does not answer the question when the model was
# actually blocked or cut off.


async def test_a_truncated_candidate_is_not_an_answer() -> None:
    """Read off the wire: `maxOutputTokens: 1` returns `parts: [{"text": ""}]` with
    `finishReason: MAX_TOKENS`. The empty string is present, well-formed, and not an
    answer."""
    truncated: dict[str, object] = {
        "candidates": [
            {"content": {"parts": [{"text": ""}], "role": "model"}, "finishReason": "MAX_TOKENS"}
        ],
        "usageMetadata": {"promptTokenCount": 9, "totalTokenCount": 9},
        "modelVersion": "gemini-3.6-flash",
    }

    with pytest.raises(GenerationUnavailableError) as raised:
        await provider(answers(truncated)).complete("s", "u")

    assert "MAX_TOKENS" in raised.value.message
    assert "empty answer" in raised.value.message


async def test_a_candidate_blocked_after_generation_is_not_an_answer() -> None:
    """`finishReason: SAFETY` and no `content` at all — the candidate exists, the answer
    does not."""
    blocked: dict[str, object] = {
        "candidates": [{"finishReason": "SAFETY", "index": 0, "safetyRatings": []}],
        "modelVersion": "gemini-3.6-flash",
    }

    with pytest.raises(GenerationUnavailableError) as raised:
        await provider(answers(blocked)).complete("s", "u")

    assert "SAFETY" in raised.value.message


async def test_a_prompt_blocked_before_generation_says_so() -> None:
    """No `candidates` member at all, and the reason in `promptFeedback`. Distinguished
    from a wrong URL, because they need different actions from whoever reads it."""
    refused: dict[str, object] = {"promptFeedback": {"blockReason": "SAFETY", "safetyRatings": []}}

    with pytest.raises(GenerationUnavailableError) as raised:
        await provider(answers(refused)).complete("s", "u")

    assert "blocked" in raised.value.message
    assert "SAFETY" in raised.value.message


async def test_a_200_with_the_wrong_shape_says_what_is_wrong() -> None:
    """The normal failure when someone points this at a URL that is nearly right. "KeyError:
    candidates" three frames down would not help them."""
    with pytest.raises(GenerationUnavailableError, match="generateContent"):
        await provider(answers({"status": "ok"})).complete("s", "u")


async def test_the_models_own_reasoning_is_not_the_answer() -> None:
    """A part marked `thought` is the model deliberating, not replying. Nothing here asks
    for them today — which is the argument for the filter, not against it: the day a
    generation setting turns them on, the alternative is the model's private reasoning
    presented to a customer as the answer, cited to their own documents."""
    thinking: dict[str, object] = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "The user wants a colour. I should say blue.", "thought": True},
                        {"text": "The sky is blue [1]."},
                    ]
                },
                "finishReason": "STOP",
            }
        ]
    }

    completion = await provider(answers(thinking)).complete("s", "u")

    assert completion.text == "The sky is blue [1]."


async def test_an_answer_split_across_parts_is_joined_rather_than_truncated() -> None:
    """`parts` is a list and one answer can arrive in several. Reading only the first would
    cut the answer at whatever boundary the model happened to use — and would look correct
    on every short reply."""
    split: dict[str, object] = {
        "candidates": [
            {"content": {"parts": [{"text": "Article 4 "}, {"text": "applies [1]."}]}},
        ]
    }

    completion = await provider(answers(split)).complete("s", "u")

    assert completion.text == "Article 4 applies [1]."


# --- What it reports -------------------------------------------------------------------


async def test_it_records_the_model_google_says_it_ran() -> None:
    """`gemini-flash-latest` is an alias. `queries.model_used` exists to record which model
    actually answered, so `modelVersion` wins over what was asked for."""
    completion = await provider(answers(LIVE_SUCCESS), model="gemini-flash-latest").complete(
        "s", "u"
    )

    assert completion.model == "gemini-3.6-flash"


def test_thinking_tokens_are_counted_as_output_because_they_are_billed_as_output() -> None:
    """The live counts from a four-word reply: 15 prompt, 5 candidate, 316 thought.

    Reporting 5 would put a number in the cost dashboard sixty times too small, and a
    confidently wrong figure in a cost report is worse than none — somebody budgets against
    it. `totalTokenCount` is not used: it is prompt and output together, which is not what
    either column holds.
    """
    assert usage_of(LIVE_SUCCESS) == (15, 321)


def test_a_response_that_reports_nothing_gives_none_rather_than_zero() -> None:
    """An unknown cost and a zero cost are different answers and only one is ever true."""
    assert usage_of({"candidates": []}) == (None, None)
    # A truncated response omits `candidatesTokenCount` entirely; inventing a zero for it
    # would be the same lie in the other direction.
    assert usage_of({"usageMetadata": {"promptTokenCount": 9, "totalTokenCount": 9}}) == (9, None)
    assert usage_of({"usageMetadata": {"promptTokenCount": "15"}}) == (None, None)


# --- Streaming -------------------------------------------------------------------------


async def test_it_streams_the_answer_and_reports_the_cost_after_it() -> None:
    """The framing is real SSE with `alt=sse`, and every chunk carries the whole
    `usageMetadata` so far — so the counts are overwritten rather than accumulated and the
    last write is the authoritative one."""
    gemini = provider(
        sse(
            {"candidates": [{"content": {"parts": [{"text": "The three primary "}]}}]},
            {"candidates": [{"content": {"parts": [{"text": "colours are red, blue "}]}}]},
            {
                "candidates": [
                    {"content": {"parts": [{"text": "and yellow [1]."}]}, "finishReason": "STOP"}
                ],
                "usageMetadata": {
                    "promptTokenCount": 12,
                    "candidatesTokenCount": 11,
                    "thoughtsTokenCount": 135,
                },
            },
        )
    )

    pieces = [piece async for piece in gemini.stream("s", "u")]

    assert "".join(pieces) == "The three primary colours are red, blue and yellow [1]."
    assert gemini.last_usage == (12, 146)


async def test_a_trailing_chunk_with_no_text_is_skipped_rather_than_yielded() -> None:
    """Read off the wire: the last chunk of a real stream is `"text": ""` beside a
    `thoughtSignature`. Yielding it would put an empty token into the chat."""
    gemini = provider(
        sse(
            {"candidates": [{"content": {"parts": [{"text": "An answer [1]."}]}}]},
            {"candidates": [{"content": {"parts": [{"text": "", "thoughtSignature": "Eq…"}]}}]},
        )
    )

    assert [piece async for piece in gemini.stream("s", "u")] == ["An answer [1]."]


async def test_a_stream_that_says_nothing_raises_rather_than_ending_quietly() -> None:
    """Silence reaching the binder is an uncited answer, discarded, and reported as an
    abstention — the product saying the corpus does not answer the question when the model
    actually refused. The reason arrives in the last chunk, not in a status code."""
    gemini = provider(sse({"candidates": [{"finishReason": "SAFETY", "index": 0}]}))

    with pytest.raises(GenerationUnavailableError) as raised:
        async for _ in gemini.stream("s", "u"):
            pass

    assert "SAFETY" in raised.value.message


async def test_a_streamed_refusal_says_as_much_as_a_buffered_one() -> None:
    """`client.stream` has not touched the body when the status arrives, so this branch has
    to read it explicitly or it reports the status and nothing else — on the endpoint the
    chat actually uses."""

    def depleted(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json=[{"error": {"message": "Your prepayment credits are depleted."}}]
        )

    with pytest.raises(GenerationUnavailableError) as raised:
        async for _ in provider(depleted).stream("s", "u"):
            pass

    assert "prepayment credits are depleted" in raised.value.message
