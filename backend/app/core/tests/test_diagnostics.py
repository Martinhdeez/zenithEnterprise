"""Two properties this tool cannot lose, and one it is easy to think it has.

Its output goes into support tickets, so it must never print a secret. And it runs when
the installation is broken, so a check that raises instead of reporting is a diagnostic
that is absent exactly when it is needed.
"""

import json

import pytest
from typer.testing import CliRunner

from app.cli import app
from app.core.config import settings
from app.core.diagnostics import redact, run_diagnostics

runner = CliRunner()


def test_redact_hides_the_password_and_keeps_the_rest() -> None:
    """User, host, port and database are what someone needs to spot a misconfiguration.
    The password is the one part that must not reach a ticket."""
    redacted = redact("postgresql+psycopg://zenith_app:hunter2@db.internal:5432/zenith")

    assert "hunter2" not in redacted
    assert redacted == "postgresql+psycopg://zenith_app:***@db.internal:5432/zenith"


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://user:p@ssw0rd!@host:5432/db",
        "postgresql+psycopg://user:@host:5432/db",
        "postgresql+psycopg://host:5432/db",
        "http://localhost:8081",
    ],
)
def test_redaction_survives_awkward_urls(url: str) -> None:
    """A password containing `@`, an empty password, no credentials at all, and a plain
    HTTP endpoint. None may raise, and none may leak."""
    result = redact(url)

    assert "ssw0rd" not in result
    assert "host" in result or "localhost" in result


@pytest.mark.asyncio
async def test_no_check_reports_the_jwt_secret(configured_engines: None) -> None:
    """The secret is never printed — not even truncated.

    A prefix narrows a brute force and tells the reader nothing they can act on. Whether
    it is set is already guaranteed: the process could not have started otherwise.
    """
    checks = await run_diagnostics()
    output = " ".join(check.detail for check in checks)

    assert settings.jwt_secret not in output
    assert settings.jwt_secret[:8] not in output


@pytest.mark.asyncio
async def test_no_check_reports_a_database_password(configured_engines: None) -> None:
    from conftest import APP_PASSWORD

    checks = await run_diagnostics()
    output = " ".join(check.detail for check in checks)

    assert APP_PASSWORD not in output


@pytest.mark.asyncio
async def test_every_check_runs_even_when_the_database_is_unreachable() -> None:
    """The moment this tool matters most.

    A diagnostic that raises on a dead database reports nothing about the parts that are
    still working — and "which parts still work" is the whole question. Every check has to
    come back, whatever it comes back with.
    """
    from app.core.database import configure_engine, configure_owner_engine

    dead = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/nowhere"
    configure_engine(dead)
    configure_owner_engine(dead)

    checks = await run_diagnostics()

    assert len(checks) == 9
    assert any(check.status == "fail" for check in checks)
    # And the failure still says nothing it should not.
    assert "nothing" not in " ".join(check.detail for check in checks)


@pytest.mark.asyncio
async def test_a_driver_error_quoting_the_password_is_scrubbed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leak path `redact` does not cover.

    `redact` handles connection strings we format ourselves. It cannot help with the ones
    we do not control: a driver that fails to connect quotes fragments of the URL back,
    and that text goes straight into the report. Observed in practice — psycopg reported
    `failed to resolve host 'p4ss@localhost'` for a password containing `@`, which is a
    perfectly ordinary generated password.

    So known secret values are removed by exact match, wherever they appear and whoever
    put them there.
    """
    from app.core.database import configure_engine, configure_owner_engine

    # A password with `@` in it, which is what made the driver misparse and echo.
    monkeypatch.setattr(
        settings, "database_url", "postgresql+psycopg://u:s3cr3t@p4ss@localhost:1/x"
    )
    monkeypatch.setattr(settings, "database_owner_url", settings.database_url)
    configure_engine(settings.database_url)
    configure_owner_engine(settings.database_owner_url)

    checks = await run_diagnostics()
    output = " ".join(check.detail for check in checks)

    assert "s3cr3t" not in output
    assert "p4ss" not in output


@pytest.mark.asyncio
async def test_details_stay_on_one_line(configured_engines: None) -> None:
    """A multi-line traceback turns a readable report into a wall of text, and an operator
    skims this before deciding what to paste into a ticket."""
    from app.core.database import configure_engine

    configure_engine("postgresql+psycopg://nobody:nothing@127.0.0.1:1/nowhere")
    checks = await run_diagnostics()

    assert all("\n" not in check.detail for check in checks)
    assert all(len(check.detail) <= 200 for check in checks)


@pytest.mark.asyncio
async def test_a_healthy_installation_reports_its_state(configured_engines: None) -> None:
    checks = await run_diagnostics()
    by_name = {check.name: check for check in checks}

    assert by_name["row-level security"].status == "ok"
    assert by_name["migrations"].status == "ok", by_name["migrations"].detail
    assert by_name["extensions"].status == "ok", by_name["extensions"].detail
    # No vector space until F6. A warning, because an installation that has not indexed
    # anything is not broken.
    assert by_name["vector space"].status == "warn"


def test_json_output_is_parseable(configured_engines: None) -> None:
    """`--json` exists so an operator can attach the result to a ticket rather than
    reformatting a terminal dump."""
    result = runner.invoke(app, ["diagnose", "--json"])

    payload = json.loads(result.output)
    assert {check["name"] for check in payload["checks"]} >= {"migrations", "row-level security"}


def test_the_exit_code_reports_failure(configured_engines: None) -> None:
    """Non-zero on any failure, so this doubles as a smoke test after an install.

    The model services are not running in the test environment, so this run fails — which
    is exactly the signal an operator who forgot to start them needs.
    """
    result = runner.invoke(app, ["diagnose"])

    assert result.exit_code == 1
    assert "FAIL" in result.output
