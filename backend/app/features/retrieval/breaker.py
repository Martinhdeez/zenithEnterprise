"""Stop paying for a component that is not answering.

F11 measured the problem on real hardware. When the reranker cannot keep up, every query
spends the full 5-second timeout waiting for it and then returns the fused order anyway:

    reranking off        1,152 ms
    reranking failing    6,237 ms   ← same answer, five seconds later

The degraded path is *slower than not having a reranker at all*. The timeout protects the
answer's correctness and does nothing for its latency, and on a `cpu` profile deployed to a
box that cannot sustain it, that is every query for as long as the misconfiguration lasts.

A breaker converts that from a permanent tax into a brief one: after a few consecutive
failures the reranker is skipped outright, and after a cooling-off period one request is
allowed through to see whether it recovered.

**Deliberately per-process and in memory.** A shared breaker would need Redis or a table,
and a wrong shared state is worse than a right local one: with several workers, each
discovers the reranker is down within a few requests of its own, which costs a handful of
slow queries and nothing else. Persisting it would add a dependency to solve a problem
measured in seconds.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

import structlog

log = structlog.get_logger()

# Three, not one. A single timeout is a hiccup — a cold model, a slow batch, a GC pause —
# and tripping on it would disable a working reranker for every user for a minute. Three in
# a row is a pattern.
FAILURES_TO_OPEN = 3

# Long enough that a restarting TEI has finished loading its model (F5: tens of seconds),
# short enough that recovery is not something an operator has to wait out.
COOLDOWN_SECONDS = 60.0


class State(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"


@dataclass
class Breaker:
    """Closed, open, half-open — and the reason the middle state is not a boolean.

    `open` means "do not call, and say why". `half-open` means "call once, and let the
    result decide". Collapsing them would either retry on every request while open, which
    is the tax this exists to remove, or never retry at all, which turns one bad minute
    into a permanent outage requiring a restart.
    """

    failures_to_open: int = FAILURES_TO_OPEN
    cooldown: float = COOLDOWN_SECONDS
    _failures: int = 0
    _opened_at: float | None = field(default=None)
    # Injected so tests do not sleep. A breaker tested with real time is a test that is
    # either slow or flaky, and usually both.
    _now: Callable[[], float] = field(default=time.monotonic)

    @property
    def state(self) -> State:
        if self._opened_at is None:
            return State.CLOSED
        elapsed = self._now() - self._opened_at
        return State.HALF_OPEN if elapsed >= self.cooldown else State.OPEN

    def allows(self) -> bool:
        """Whether this request should attempt the reranker."""
        return self.state is not State.OPEN

    def succeeded(self) -> None:
        """Reset completely.

        A success in `half-open` closes the breaker outright rather than decrementing the
        count. The component answered; carrying a grudge from before the outage would make
        the next isolated hiccup trip it early.
        """
        if self._opened_at is not None:
            log.info("reranker_recovered")
        self._failures = 0
        self._opened_at = None

    def failed(self) -> None:
        """Count a failure, and open if this is the third in a row.

        A failure while `half-open` re-opens immediately: the probe was the retry, and
        retrying again straight away would be the tax returning under another name.
        """
        if self.state is State.HALF_OPEN:
            self._opened_at = self._now()
            log.warning("reranker_still_down", cooldown=self.cooldown)
            return

        self._failures += 1
        if self._failures >= self.failures_to_open:
            self._opened_at = self._now()
            log.warning("reranker_circuit_opened", failures=self._failures)
