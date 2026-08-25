"""The limits from mvp.md 2.12, on the endpoint that actually costs something.

F9 measured ~9 seconds of model time per answer and F11 measured the hardware underneath
it: four cores, where ten concurrent *searches* already take 5.2 seconds each. Ten
concurrent generations do not degrade this service, they remove it — and with streaming,
thirty tabs left open on a dashboard that retries is an outage nobody meant to cause.
"""

from uuid import uuid4

import pytest

from app.core.config import settings
from app.features.auth.access.permissions import CATALOGUE
from app.features.auth.service import AccessProfile
from app.features.query.throttle import QueryRateLimitedError, reset, throttle_query
from app.features.tenancy.context import TenantContext


@pytest.fixture(autouse=True)
def fresh_limits() -> None:
    reset()


def profile(tenant_id: object | None = None, user_id: object | None = None) -> AccessProfile:
    tenant = tenant_id or uuid4()
    return AccessProfile(
        user_id=user_id or uuid4(),  # type: ignore[arg-type]
        context=TenantContext.for_tenant(tenant),  # type: ignore[arg-type]
        permissions=frozenset(CATALOGUE),
    )


def test_a_normal_rate_is_allowed() -> None:
    caller = profile()

    for _ in range(settings.max_queries_per_minute_user):
        throttle_query(caller)  # type: ignore[arg-type]


def test_one_user_exceeding_their_allowance_is_refused() -> None:
    caller = profile()
    for _ in range(settings.max_queries_per_minute_user):
        throttle_query(caller)  # type: ignore[arg-type]

    with pytest.raises(QueryRateLimitedError) as raised:
        throttle_query(caller)  # type: ignore[arg-type]

    assert raised.value.status_code == 429
    assert raised.value.retry_after == 60, "a 429 must say when to come back, not only no"


def test_one_user_cannot_exhaust_the_service_for_their_colleagues() -> None:
    """The per-user limit is what makes a runaway tab one person's problem."""
    tenant = uuid4()
    noisy = profile(tenant_id=tenant)
    quiet = profile(tenant_id=tenant)

    for _ in range(settings.max_queries_per_minute_user):
        throttle_query(noisy)  # type: ignore[arg-type]

    with pytest.raises(QueryRateLimitedError):
        throttle_query(noisy)  # type: ignore[arg-type]
    throttle_query(quiet)  # type: ignore[arg-type]  # unaffected


def test_a_tenant_can_be_limited_by_many_users_at_once() -> None:
    """The window the per-user limit cannot see.

    Twenty people each well inside their own allowance still add up to more than four
    cores can serve, and the per-user limit is blind to that by construction.
    """
    tenant = uuid4()
    callers = [profile(tenant_id=tenant) for _ in range(20)]

    sent = 0
    with pytest.raises(QueryRateLimitedError) as raised:
        while True:
            throttle_query(callers[sent % len(callers)])  # type: ignore[arg-type]
            sent += 1

    assert sent == settings.max_queries_per_minute_tenant
    assert "organisation" in raised.value.message


def test_another_tenant_is_unaffected() -> None:
    """Isolation applies to allowances too. One customer exhausting theirs must not deny
    the service to another on the same installation."""
    busy = uuid4()
    for index in range(settings.max_queries_per_minute_tenant):
        throttle_query(profile(tenant_id=busy, user_id=uuid4() if index % 5 == 0 else None))  # type: ignore[arg-type]

    throttle_query(profile())  # type: ignore[arg-type]


def test_a_refused_request_does_not_spend_the_users_allowance() -> None:
    """Both windows are checked before either is charged.

    Otherwise a user near their limit would be penalised by their colleagues' traffic:
    the tenant window refuses the request, but the user's allowance was already spent on
    it, so they are pushed over a limit they never reached themselves.
    """
    tenant = uuid4()
    filler = [profile(tenant_id=tenant) for _ in range(20)]
    for index in range(settings.max_queries_per_minute_tenant):
        throttle_query(filler[index % len(filler)])  # type: ignore[arg-type]

    newcomer = profile(tenant_id=tenant)
    for _ in range(3):
        with pytest.raises(QueryRateLimitedError):
            throttle_query(newcomer)  # type: ignore[arg-type]

    # Their own window is untouched: every refusal came from the tenant limit.
    from app.features.query.throttle import user_limiter

    assert user_limiter.would_allow(str(newcomer.user_id)) is True
