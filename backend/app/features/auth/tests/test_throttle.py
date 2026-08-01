"""The defence that stops login from being a denial-of-service button.

Argon2 is expensive on purpose, and §2.3 runs it even for addresses that do not exist
so that timing cannot be used to enumerate accounts. Without a limit in front, those two
correct decisions combine into a way for anyone to exhaust the server's CPU and RAM.
"""

from app.features.auth.throttle import SlidingWindowLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_the_allowance_is_spent_and_then_refused() -> None:
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60.0, clock=FakeClock())

    assert [limiter.allow("1.2.3.4") for _ in range(4)] == [True, True, True, False]


def test_callers_do_not_share_an_allowance() -> None:
    """One noisy address must not lock everyone else out — that would turn the defence
    into the outage it exists to prevent."""
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60.0, clock=FakeClock())

    assert limiter.allow("1.2.3.4")
    assert not limiter.allow("1.2.3.4")
    assert limiter.allow("5.6.7.8")


def test_the_window_slides_rather_than_resetting() -> None:
    """A fixed window lets a caller spend the whole allowance at the end of one window
    and again at the start of the next — twice the intended rate, precisely when someone
    is probing for the edges."""
    clock = FakeClock()
    limiter = SlidingWindowLimiter(limit=2, window_seconds=60.0, clock=clock)

    clock.now = 0.0
    assert limiter.allow("1.2.3.4")
    clock.now = 59.0
    assert limiter.allow("1.2.3.4")
    assert not limiter.allow("1.2.3.4")

    # The first hit has aged out, the second has not: one slot back, not two.
    clock.now = 61.0
    assert limiter.allow("1.2.3.4")
    assert not limiter.allow("1.2.3.4")


def test_keys_do_not_accumulate_without_bound() -> None:
    """An attacker rotating source addresses must not be able to grow the limiter
    itself. Trading a CPU exhaustion for a memory exhaustion is not a fix."""
    clock = FakeClock()
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60.0, max_keys=100, clock=clock)

    for index in range(200):
        clock.now = float(index)
        limiter.allow(f"10.0.0.{index}")

    assert limiter.tracked_keys <= 100
