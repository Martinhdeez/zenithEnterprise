"""Rate limiting the expensive endpoint (mvp.md 2.12).

Deferred to M4 every time it came up, and three things have since changed that move it to
the front.

**Generation is expensive in a way search never was.** F9 measured ~9 seconds of model time
per answer. F11 measured the hardware: four cores, where ten concurrent *searches* already
cost 5.2 seconds each. Ten concurrent generations do not degrade the service, they remove
it.

**Streaming holds the connection for the whole answer.** `POST /query/stream` occupies a
worker and a socket for ten seconds or more, so thirty tabs left open on a dashboard that
retries is an outage produced by nobody being malicious.

**The defence already exists.** `SlidingWindowLimiter` was written for login, for the same
reason wearing different clothes: argon2 is deliberately expensive, so an unauthenticated
flood was a denial of service we had written ourselves. Generation is that shape again —
expensive by design, triggered by a cheap request.

Two windows rather than one, because they fail differently. Per user stops one person's
runaway tab; per tenant stops twenty people's, which the per-user limit cannot see. That is
the same argument `throttle.py` makes for pairing a rate limit with a semaphore.

Per process, and stated plainly: with several uvicorn workers the effective limit is
multiplied by the worker count and both reset on restart. Honest for the single-node
install this product ships as. A shared limiter needs Redis and belongs with the decision
to run more than one node, not with this.
"""

from fastapi import Depends

from app.common.exceptions import RateLimitedError
from app.core.config import settings
from app.features.auth.dependencies import CurrentProfile
from app.features.auth.throttle import SlidingWindowLimiter

WINDOW_SECONDS = 60.0

user_limiter = SlidingWindowLimiter(
    limit=settings.max_queries_per_minute_user, window_seconds=WINDOW_SECONDS
)
tenant_limiter = SlidingWindowLimiter(
    limit=settings.max_queries_per_minute_tenant, window_seconds=WINDOW_SECONDS
)


class QueryRateLimitedError(RateLimitedError):
    """A 429 that tells the caller when to come back.

    `Retry-After` is not decoration. A client told only "no" retries immediately and makes
    the overload worse; one told "in 60 seconds" can obey. §2.12 requires the header for
    that reason.
    """

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def throttle_query(profile: CurrentProfile) -> None:
    """Check both windows before the request is allowed to cost anything.

    The user's window is checked first so that one person's runaway client is attributed to
    them rather than reported as the tenant being busy — the message a customer reads
    should point at what they can actually fix.

    Both windows are *consumed* only when both allow. Checking the tenant window after the
    user window has already recorded a hit would charge the user for a request the tenant
    limit then refused.
    """
    user_key = str(profile.user_id)
    tenant_key = str(profile.context.tenant_id)

    if not user_limiter.would_allow(user_key):
        raise QueryRateLimitedError(
            f"You have asked more than {settings.max_queries_per_minute_user} questions in a "
            f"minute. Try again shortly.",
            retry_after=int(WINDOW_SECONDS),
        )
    if not tenant_limiter.would_allow(tenant_key):
        raise QueryRateLimitedError(
            f"Your organisation has asked more than "
            f"{settings.max_queries_per_minute_tenant} questions in a minute. Try again "
            f"shortly.",
            retry_after=int(WINDOW_SECONDS),
        )

    user_limiter.allow(user_key)
    tenant_limiter.allow(tenant_key)


#: Applied to `/query` and `/query/stream` only. `/search` costs ~1.2 s of mostly-Postgres
#: and is not what falls over; limiting it would restrict the cheap path to protect the
#: expensive one.
RateLimit = Depends(throttle_query)


def reset() -> None:
    """Forget every caller. For tests, which share one process."""
    user_limiter.reset()
    tenant_limiter.reset()
