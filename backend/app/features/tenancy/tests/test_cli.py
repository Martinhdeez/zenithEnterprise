"""The cold-start path.

Without this, the product has a login for users that nothing can create. The spec is
explicit that provisioning must not be a hand-written INSERT in `psql` (mvp.md §2.4), so
these tests run the real commands the way an installer will.

They exercise the CLI end to end rather than its internals: the failure this is guarding
against is the whole path being broken — a missing entry point, a command that never
commits — and none of that shows up in a unit test of a helper.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from app.cli import app
from app.common.exceptions import AuthenticationError
from app.core.database import dispose_engines
from app.features.auth.service import AuthService

runner = CliRunner()


def _run[T](work: Coroutine[Any, Any, T]) -> T:
    """Run an assertion in its own loop, then release the connections.

    These tests are synchronous because the CLI itself calls `asyncio.run`, and that
    cannot happen inside a loop pytest-asyncio has already started. The disposal mirrors
    what the CLI does for the same reason: engines belong to the loop that made them.
    """

    async def runner_() -> T:
        try:
            return await work
        finally:
            await dispose_engines()

    return asyncio.run(runner_())


def _password_from(output: str) -> str:
    """The generated password is printed once and never stored, so the test reads it the
    same way an installer does."""
    line = next(line for line in output.splitlines() if "password:" in line)
    return line.split("password:")[1].strip()


def test_create_tenant_produces_a_usable_administrator(configured_engines: None) -> None:
    """The whole point of the command: after it runs, someone can log in.

    Asserted through `AuthService`, not by reading the row back, because a hash the
    application cannot verify is a successful-looking install that nobody can enter.
    """
    name = f"CLI {uuid4()}"
    email = f"admin-{uuid4()}@example.com"

    result = runner.invoke(app, ["create-tenant", name, "--admin-email", email])

    assert result.exit_code == 0, result.output
    password = _password_from(result.output)
    pair = _run(AuthService().authenticate(email, password))
    assert pair.access_token

    profile = _run(AuthService().profile(*AuthService().principal(pair.access_token)))
    # Born able to administer: without the full catalogue the first administrator cannot
    # invite anyone, and the installation is a dead end.
    assert "users.manage" in profile.permissions
    assert "roles.manage" in profile.permissions


def test_the_password_is_never_taken_as_an_argument() -> None:
    """An argument lands in the shell history and in the process table, where every
    other user of the machine can read it. The command must not accept one at all."""
    result = runner.invoke(app, ["create-tenant", "--help"])

    assert "--password" not in result.output


def test_a_duplicate_tenant_fails_cleanly(configured_engines: None) -> None:
    """Whoever sees this is installing the product. A driver traceback is not an error
    message."""
    name = f"CLI dup {uuid4()}"
    runner.invoke(app, ["create-tenant", name, "--admin-email", f"a-{uuid4()}@example.com"])

    result = runner.invoke(
        app, ["create-tenant", name, "--admin-email", f"b-{uuid4()}@example.com"]
    )

    assert result.exit_code == 1
    assert "already exists" in result.output
    assert "Traceback" not in result.output


def test_invite_creates_a_user_with_the_named_role(configured_engines: None) -> None:
    name = f"CLI invite {uuid4()}"
    runner.invoke(app, ["create-tenant", name, "--admin-email", f"admin-{uuid4()}@example.com"])
    email = f"member-{uuid4()}@example.com"

    result = runner.invoke(app, ["invite", "--tenant", name, "--email", email, "--role", "member"])

    assert result.exit_code == 0, result.output
    pair = _run(AuthService().authenticate(email, _password_from(result.output)))
    profile = _run(AuthService().profile(*AuthService().principal(pair.access_token)))
    assert profile.permissions == {"query.execute", "query.history.own"}


def test_invite_refuses_an_unknown_role(configured_engines: None) -> None:
    """Silently creating a user with no role would produce an account that can log in and
    do nothing, which looks like a bug in the application rather than a typo here."""
    name = f"CLI role {uuid4()}"
    runner.invoke(app, ["create-tenant", name, "--admin-email", f"admin-{uuid4()}@example.com"])

    result = runner.invoke(
        app,
        [
            "invite",
            "--tenant",
            name,
            "--email",
            f"x-{uuid4()}@example.com",
            "--role",
            "nonexistent",
        ],
    )

    assert result.exit_code == 1
    assert "no role named" in result.output


def test_reset_password_replaces_the_password_and_kills_sessions(
    configured_engines: None,
) -> None:
    """The recovery path for an on-premise install with no outbound mail.

    It must also invalidate live sessions: whoever needs a reset may have had their
    account compromised, and a reset that leaves the attacker's session working is
    cosmetic.
    """
    name = f"CLI reset {uuid4()}"
    email = f"admin-{uuid4()}@example.com"
    created = runner.invoke(app, ["create-tenant", name, "--admin-email", email])
    original = _password_from(created.output)
    before = _run(AuthService().authenticate(email, original))

    result = runner.invoke(app, ["reset-password", "--tenant", name, "--email", email])

    assert result.exit_code == 0, result.output
    new_password = _password_from(result.output)
    assert new_password != original

    _run(AuthService().authenticate(email, new_password))
    with pytest.raises(AuthenticationError, match="invalid email or password"):
        _run(AuthService().authenticate(email, original))
    with pytest.raises(AuthenticationError, match="no longer valid"):
        _run(AuthService().refresh(before.refresh_token))


def test_list_tenants_reports_what_exists(configured_engines: None) -> None:
    name = f"CLI list {uuid4()}"
    runner.invoke(app, ["create-tenant", name, "--admin-email", f"admin-{uuid4()}@example.com"])

    result = runner.invoke(app, ["list-tenants"])

    assert result.exit_code == 0
    assert name in result.output
    assert "admin" in result.output


def test_generate_secret_output_satisfies_the_startup_guard() -> None:
    """The command exists to remove the operator's choice, so what it prints has to be
    accepted by the validator that rejected their previous attempt."""
    from app.core.config import Settings

    result = runner.invoke(app, ["generate-secret"])

    assert result.exit_code == 0
    assert Settings(jwt_secret=result.output.strip()).jwt_secret  # type: ignore[call-arg]
