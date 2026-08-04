"""The state machine, and the query latency it exists to stop paying.

F11's measurement is the whole justification: a reranker that times out costs the full
5-second timeout on every query and returns the fused order anyway — 6,237 ms for an answer
that takes 1,152 ms with reranking switched off. These tests pin the transitions that turn
that permanent tax into a one-minute one.

Time is injected rather than slept. A breaker tested against the real clock is a test that
is either slow or flaky, and usually both.
"""

import httpx

from app.core.hardware import PROFILES
from app.features.retrieval.breaker import Breaker, State
from app.features.retrieval.service import SearchService
from conftest import Account, WorkingEmbedder

from .test_rerank import QUERY, reranker
from .test_search import profile_for, seed


class Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def unreachable(request: httpx.Request) -> httpx.Response:
    """Never called: the circuit is open, so the reranker is skipped entirely."""
    raise AssertionError("the reranker must not be called while the circuit is open")


def breaker(clock: Clock, failures: int = 3, cooldown: float = 60.0) -> Breaker:
    return Breaker(failures_to_open=failures, cooldown=cooldown, _now=clock)


def test_it_starts_closed_and_allows_requests() -> None:
    assert breaker(Clock()).state is State.CLOSED
    assert breaker(Clock()).allows() is True


def test_one_failure_does_not_open_it() -> None:
    """A single timeout is a hiccup — a cold model, a slow batch, a GC pause. Tripping on
    it would disable a working reranker for every user for a minute."""
    circuit = breaker(Clock())

    circuit.failed()

    assert circuit.state is State.CLOSED
    assert circuit.allows() is True


def test_three_consecutive_failures_open_it() -> None:
    circuit = breaker(Clock())

    for _ in range(3):
        circuit.failed()

    assert circuit.state is State.OPEN
    assert circuit.allows() is False


def test_a_success_resets_the_count() -> None:
    """Consecutive, not cumulative. Two failures an hour apart with successes between them
    describe a healthy service, and counting them together would trip on nothing."""
    circuit = breaker(Clock())

    circuit.failed()
    circuit.failed()
    circuit.succeeded()
    circuit.failed()

    assert circuit.state is State.CLOSED


def test_it_goes_half_open_after_the_cooldown() -> None:
    """Open must not be permanent. Otherwise one bad minute becomes an outage that needs a
    restart to clear."""
    clock = Clock()
    circuit = breaker(clock)
    for _ in range(3):
        circuit.failed()

    clock.advance(61)

    assert circuit.state is State.HALF_OPEN
    assert circuit.allows() is True, "one probe must be let through"


def test_a_successful_probe_closes_it_completely() -> None:
    """Closes outright rather than decrementing: the component answered, and carrying a
    grudge from before the outage would trip it early on the next isolated hiccup."""
    clock = Clock()
    circuit = breaker(clock)
    for _ in range(3):
        circuit.failed()
    clock.advance(61)

    circuit.succeeded()

    assert circuit.state is State.CLOSED


def test_a_failed_probe_reopens_immediately() -> None:
    """Without this the probe would be followed by two more full-price attempts before the
    breaker reopened — the tax returning under another name."""
    clock = Clock()
    circuit = breaker(clock)
    for _ in range(3):
        circuit.failed()
    clock.advance(61)
    assert circuit.state is State.HALF_OPEN

    circuit.failed()

    assert circuit.state is State.OPEN


async def test_an_open_circuit_skips_the_reranker_entirely(account: Account) -> None:
    """The end-to-end point of all of this: the call is not made at all.

    Asserted by counting requests rather than by timing, because a test that measures
    seconds measures the machine it runs on.
    """
    await seed(account.tenant_id, account.default_label)
    calls = 0

    def dead(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("too slow")

    circuit = breaker(Clock())
    profile = await profile_for(account)

    for _ in range(5):
        result = await SearchService(
            profile,
            embedder=WorkingEmbedder(),  # type: ignore[arg-type]
            hardware=PROFILES["cpu"],
            reranker=reranker(dead),
            breaker=circuit,
        ).search(QUERY)
        assert result.hits, "the search must keep answering throughout"

    assert calls == 3, "three attempts, then the circuit stops paying for them"
    assert circuit.state is State.OPEN


async def test_the_answer_still_says_it_is_degraded_while_open(account: Account) -> None:
    """Skipping the reranker is not the same as not having one. The customer paid for
    better ordering and is getting the fused order, so the response says so — otherwise the
    breaker would convert a visible failure into a silent one, which is the exact trade this
    project refuses everywhere else."""
    await seed(account.tenant_id, account.default_label)
    circuit = breaker(Clock())
    for _ in range(3):
        circuit.failed()

    result = await SearchService(
        await profile_for(account),
        embedder=WorkingEmbedder(),  # type: ignore[arg-type]
        hardware=PROFILES["cpu"],
        reranker=reranker(unreachable),
        breaker=circuit,
    ).search(QUERY)

    assert result.degraded is True
    assert "circuit open" in (result.reason or "")
