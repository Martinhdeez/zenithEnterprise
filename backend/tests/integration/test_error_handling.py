"""What a client sees when something goes wrong that nobody planned for.

Domain errors are covered wherever they are raised. This covers the other half — the
exception no one anticipated — because that is the one whose response shape was never
decided, and an undecided shape is decided by the deployment.

Driven against the handlers directly rather than through a client, and not for convenience.
Starlette's `ServerErrorMiddleware` sends the response and then **re-raises**, so that a
real server logs the failure; over `ASGITransport` that re-raise lands in the test instead
of the response. The remaining option, `TestClient`, is untyped under the `httpx`
deprecation and would cost thirteen `Unknown` errors under strict pyright. Calling the
handler is what actually decides the body, so it is what the test calls — with a separate
case asserting the application has it registered, since a perfect handler nobody installed
is worth nothing.
"""

import json

from fastapi import FastAPI
from starlette.requests import Request

from app.common.exceptions import NotFoundError, ZenithError
from app.main import app, handle_domain_error, handle_unexpected_error


def request(path: str = "/query") -> Request:
    return Request({"type": "http", "method": "POST", "path": path, "headers": []})


def body(response: object) -> dict[str, str]:
    raw = getattr(response, "body", b"{}")
    assert isinstance(raw, bytes)
    decoded: dict[str, str] = json.loads(raw)
    return decoded


async def test_an_unexpected_exception_returns_the_standard_error_shape() -> None:
    """A client cannot parse a body whose shape changes with how the app is served.

    Without a handler this is Starlette's default: plain text here, and an HTML traceback
    if anyone ever leaves `debug=True` on a customer's server.
    """
    response = await handle_unexpected_error(request(), RuntimeError("anything"))

    assert response.status_code == 500
    assert body(response) == {
        "code": "internal_error",
        "message": "An unexpected error occurred.",
    }


async def test_the_exception_text_never_reaches_the_caller() -> None:
    """The reason the message is fixed and useless to the caller.

    Exception text on this codebase quotes connection strings, query text and occasionally
    the row that caused the failure. Deciding what is safe to reveal per-exception is a
    judgement nobody makes correctly at three in the morning, so it is made once, here.
    """
    leaky = RuntimeError("connection postgresql://zenith:hunter2@db/zenith failed")

    rendered = str(body(await handle_unexpected_error(request(), leaky)))

    assert "hunter2" not in rendered
    assert "postgresql://" not in rendered
    assert "RuntimeError" not in rendered


async def test_a_domain_error_keeps_its_own_status_and_message() -> None:
    """The catch-all must not swallow the errors that were designed to be seen."""
    response = await handle_domain_error(request(), NotFoundError("no such document"))

    assert response.status_code == 404
    assert body(response) == {"code": "not_found", "message": "no such document"}


def test_the_application_installs_both_handlers() -> None:
    """A handler nobody registered is worth nothing, and the registration is one line in
    `main.py` that no other test would miss."""
    assert isinstance(app, FastAPI)
    assert app.exception_handlers[ZenithError] is handle_domain_error
    assert app.exception_handlers[Exception] is handle_unexpected_error
