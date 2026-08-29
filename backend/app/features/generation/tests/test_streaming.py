"""The one guarantee that survives streaming, proven at the character level.

F8 deferred streaming because the citation binder needs the whole answer before it can
strip an invalid marker. `MarkerFilter` is the answer to half of that: a marker is short
and self-delimiting, so it can be validated in flight. The other half — an answer that
cites nothing valid — is knowable only at the end and is handled by the final `result`
event, which `test_generation.py` covers.

The tests feed text one character at a time in places, because that is the hard case: a
model streams `[`, `1`, `2`, `]` as four tokens, and any implementation that needs the
whole marker present in one chunk would never match one.
"""

from collections.abc import AsyncIterator

from app.features.generation.answering.streaming import MAX_MARKER, MarkerFilter, filtered

VALID = frozenset({1, 2, 3})


def run(chunks: list[str], valid: frozenset[int] = VALID) -> tuple[str, MarkerFilter]:
    marker_filter = MarkerFilter(valid=valid)
    out = "".join(marker_filter.feed(chunk) for chunk in chunks)
    return out + marker_filter.flush(), marker_filter


def test_a_valid_marker_passes_through() -> None:
    text, _ = run(["The rate is 1.45% [2]."])

    assert text == "The rate is 1.45% [2]."


def test_an_invalid_marker_never_reaches_the_client() -> None:
    """The whole reason this class exists.

    Non-streaming, `citations.bind` strips the marker before anything is sent. Streaming,
    there is no "before" — so the filter has to do it in flight, or a fabricated citation
    is displayed and then retracted, which is worse than never showing it.
    """
    text, marker_filter = run(["The penalty is 4% [9] of turnover."])

    assert "[9]" not in text
    assert text == "The penalty is 4% of turnover."
    assert marker_filter.fabricated == 1


def test_a_marker_split_across_tokens_is_still_caught() -> None:
    """The case that decides the implementation.

    A model emits `[`, `9`, `]` as separate tokens. Anything that scans accumulated text
    with a regex would have already sent the `[` and the `9` before it could match.
    """
    text, marker_filter = run(["The penalty ", "is 4%", " [", "9", "]", " of turnover."])

    assert "9" not in text
    assert marker_filter.fabricated == 1


def test_the_valid_half_of_a_mixed_marker_survives() -> None:
    """`[1, 9]` becomes `[1]`, matching `citations.bind` exactly.

    The two must agree: the filter decides what is *sent* and `bind` decides what is
    *recorded*, and a client seeing a citation the audit row does not have would be a
    discrepancy nobody could explain six months later.
    """
    text, marker_filter = run(["Both apply [1, 9]."])

    assert text == "Both apply [1]."
    assert marker_filter.fabricated == 1
    assert marker_filter.cited == {1}


def test_a_bracket_in_prose_is_released_rather_than_swallowed() -> None:
    """`[see annex]` is not a citation, and holding it back would delay real text.

    Released as soon as a letter appears, rather than at the closing bracket, so the
    latency cost of a stray bracket is one character rather than a clause.
    """
    text, marker_filter = run(["Refer to [see annex] for detail."])

    assert text == "Refer to [see annex] for detail."
    assert marker_filter.fabricated == 0


def test_an_unterminated_bracket_is_flushed_at_the_end() -> None:
    """Text the model meant to write must not be eaten because it happened to be last."""
    text, _ = run(["The answer is [1] and then ["])

    assert text.endswith("[")


def test_a_runaway_bracket_does_not_swallow_the_answer() -> None:
    """Without a ceiling, one stray `[` would buffer the rest of the response and the
    client would receive nothing at all — a worse failure than a visible bracket."""
    long_run = "[" + "1" * (MAX_MARKER + 10) + " and the rest of the answer"

    text, _ = run([long_run])

    assert "the rest of the answer" in text


async def test_the_stream_transformer_preserves_order() -> None:
    """The class is used through this wrapper, so the wrapper gets its own case."""

    async def tokens() -> AsyncIterator[str]:
        for chunk in ["Alpha [1]", " beta [9]", " gamma"]:
            yield chunk

    received = [piece async for piece in filtered(tokens(), VALID)]

    assert "".join(received) == "Alpha [1] beta gamma"
