"""The embedding client, tested against the three ways M0 broke it on real hardware."""

import httpx
import pytest

from app.core.hardware import PROFILES
from app.features.embeddings.client import EmbeddingServiceError, TeiClient


def client_for(profile_name: str, handler: httpx.MockTransport | None = None) -> TeiClient:
    return TeiClient(url="http://tei", profile=PROFILES[profile_name])


def test_batches_respect_the_token_budget() -> None:
    """The 413.

    TEI ran with `--max-batch-tokens 2048`; the client sent eight chunks of roughly 350
    tokens — 2,800 — and every request failed. A batch is a token budget rather than a
    count, so dense pages and sparse pages both produce a legal request.
    """
    profile = PROFILES["low-spec"]
    client = client_for("low-spec")
    texts = ["x" * 900] * 10  # ~300 estimated tokens each

    batches = list(client.plan_batches(texts))

    assert all(
        sum(len(text) // 3 + 1 for text in batch) <= profile.max_batch_tokens for batch in batches
    )
    assert sum(len(batch) for batch in batches) == 10


def test_batches_also_respect_the_client_item_limit() -> None:
    """Two independent bounds. `max_batch_tokens` is what the server processes at once;
    `max_client_batch_size` is how many items it accepts per request."""
    client = client_for("low-spec")

    batches = list(client.plan_batches(["tiny"] * 20))

    assert max(len(batch) for batch in batches) <= PROFILES["low-spec"].max_client_batch_size


def test_an_oversized_single_text_is_sent_alone_rather_than_truncated() -> None:
    """It will fail, and it should.

    Silently truncating a customer's paragraph produces an embedding for text nobody wrote,
    and the passage becomes retrievable by content it does not contain. The chunker's size
    bound is what stops this arising; this is the behaviour if it ever does.
    """
    client = client_for("low-spec")

    batches = list(client.plan_batches(["x" * 100_000]))

    assert batches == [["x" * 100_000]]


async def test_a_rejected_batch_is_not_retried() -> None:
    """A 413 will be a 413 every time. Retrying it just fails more slowly, and the message
    has to point at the actual cause — a profile that disagrees with how TEI was started."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(413, json={"error": "batch too large"})

    client = client_for("low-spec")
    transport = httpx.MockTransport(handler)

    with pytest.raises(EmbeddingServiceError, match="ZENITH_HARDWARE"):
        async with httpx.AsyncClient(transport=transport) as http:
            await client.send_batch(http, ["one"])

    assert calls == 1


async def test_a_server_still_loading_is_retried() -> None:
    """TEI takes tens of seconds to load BGE-M3. A worker starting alongside it would
    otherwise fail every document queued in that window."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=[[0.1] * 1024])

    client = client_for("low-spec")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        vectors = await client.send_batch(http, ["one"])

    assert calls == 3
    assert len(vectors[0]) == 1024


async def test_batches_are_sent_one_at_a_time_and_in_order() -> None:
    """Exit 139.

    `--max-concurrent-requests 4` panics TEI cpu-1.8 outright: "Queue background task
    dropped the receiver... This is a bug." Concurrency is never pushed into the server;
    it is controlled on our side, because a queue we own degrades and a queue inside TEI
    crashes.

    Order matters as much as sequencing: the vectors come back as a flat list and are
    zipped against the chunks, so a reordered batch would attach every embedding to the
    wrong passage — silently, and with plausible-looking results.
    """
    in_flight = 0
    peak = 0
    received: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        inputs = list(httpx.Response(200, content=request.content).json()["inputs"])
        received.extend(inputs)
        in_flight -= 1
        return httpx.Response(200, json=[[float(len(text))] * 4 for text in inputs])

    texts = [f"passage {index} " * 50 for index in range(9)]
    client = TeiClient(
        url="http://tei", profile=PROFILES["low-spec"], transport=httpx.MockTransport(handler)
    )

    vectors = await client.embed(texts)

    assert peak == 1
    assert received == texts, "batches were reordered; embeddings would attach to the wrong chunk"
    assert len(vectors) == len(texts)
