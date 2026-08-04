"""Rate limiting for the login endpoint.

This is not a nice-to-have that can wait for M4, and the reason is specific: argon2 is
*designed* to be expensive. Default parameters cost tens of megabytes and real CPU per
verification, and §2.3 deliberately runs a verification even for addresses that do not
exist, so that response time cannot be used to enumerate accounts. Those two decisions
together mean an unauthenticated caller can make the server burn CPU and memory at will
with a loop of invented addresses. On a single VPS that is a working denial of service
written by us, not by an attacker.

Two independent defences, because they fail differently:

- A **rate limit** caps how often one caller may try. It stops the ordinary flood.
- A **semaphore** caps how many verifications run at once, whatever the source. It is
  what bounds peak memory when the flood comes from many addresses at once, which the
  rate limit alone would not catch.

Both are per process. With several uvicorn workers the effective limit is multiplied by
the worker count, and both reset on restart. That is honest for a single-node install
and is written down rather than glossed over; a shared limiter is M4, along with the
other §2.12 limits.
"""

import asyncio
import time
from collections import deque
from collections.abc import Callable

from app.core.config import settings


class SlidingWindowLimiter:
    """Per-key sliding window.

    Sliding rather than fixed: a fixed window lets a caller spend the whole allowance
    at the end of one window and again at the start of the next, which is twice the
    intended rate exactly when someone is probing for the edges.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._max_keys = max_keys
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def reset(self) -> None:
        """Forget every caller.

        For tests, which share one process and would otherwise exhaust a single shared
        allowance between them — an ordering dependency that turns a green suite red the
        moment somebody adds a test that logs in.
        """
        self._hits.clear()

    @property
    def tracked_keys(self) -> int:
        """How many callers are being remembered. Exposed because the bound on it is a
        property worth testing, not an implementation detail."""
        return len(self._hits)

    def allow(self, key: str) -> bool:
        now = self._clock()
        self._evict(now)
        hits = self._fresh(key, now)

        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True

    def would_allow(self, key: str) -> bool:
        """Whether `allow` would succeed, without spending the allowance.

        Needed wherever two limits guard one request: checking the second after the first
        has already recorded a hit charges the caller for a request the second then
        refuses, so a user near their limit would be penalised by their colleagues' traffic.
        """
        now = self._clock()
        return len(self._fresh(key, now)) < self._limit

    def _fresh(self, key: str, now: float) -> deque[float]:
        """This key's hits, with everything outside the window discarded."""
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > self._window:
            hits.popleft()
        return hits

    def _evict(self, now: float) -> None:
        """Drop keys with nothing left in the window.

        Without this the dictionary grows once per distinct source address, and an
        attacker rotating addresses turns the defence into a memory leak — trading one
        denial of service for another.
        """
        if len(self._hits) < self._max_keys:
            return
        stale = [
            key for key, hits in self._hits.items() if not hits or now - hits[-1] > self._window
        ]
        for key in stale:
            del self._hits[key]
        if len(self._hits) >= self._max_keys:
            # Still full: every key is active, which is either a very busy install or a
            # distributed attack. Start over rather than grow without bound; the cost is
            # that some callers get a fresh allowance.
            self._hits.clear()


login_limiter = SlidingWindowLimiter(
    limit=settings.login_attempts_per_minute,
    window_seconds=60.0,
)

# Bounds concurrent argon2 verifications. Each one holds `memory_cost` for its duration,
# so this is the number that decides peak RAM on the login path.
hash_slots = asyncio.Semaphore(settings.password_hash_concurrency)


async def run_hash[T](work: Callable[[], T]) -> T:
    """Run a password hash off the event loop, under the concurrency cap.

    argon2 is CPU-bound C code. Called directly in a coroutine it blocks the loop for
    its whole duration, which means one login stalls every other request in the process
    — including the health check the load balancer is watching.
    """
    async with hash_slots:
        return await asyncio.to_thread(work)
