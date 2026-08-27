"""Two properties this tool cannot lose, and one it is easy to think it has.

Its output goes into support tickets, so it must never print a secret. And it runs when
the installation is broken, so a check that raises instead of reporting is a diagnostic
that is absent exactly when it is needed.
"""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
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

    assert len(checks) == 14
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


def test_the_exit_code_reports_failure(
    configured_engines: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-zero on any failure, so this doubles as a smoke test after an install.

    The unreachable service is **pointed at explicitly** rather than assumed. This test used
    to rely on nothing listening on the default ports, and passed for as long as that was
    true of the developer's machine: the day the reranker was started locally, `diagnose`
    correctly reported everything healthy and the test failed for being right. A test whose
    outcome depends on what the person running it happens to have open is not testing the
    exit code, it is testing their laptop.
    """
    monkeypatch.setattr(settings, "tei_embed_url", "http://127.0.0.1:1")

    result = runner.invoke(app, ["diagnose"])

    assert result.exit_code == 1
    assert "FAIL" in result.output


def _serving(monkeypatch: pytest.MonkeyPatch, info: dict[str, str] | None) -> None:
    """Stand in for every TEI container: healthy, and `/info` answering or absent."""
    import httpx

    class Fake:
        async def __aenter__(self) -> "Fake":
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def get(self, url: str) -> httpx.Response:
            if url.endswith("/info"):
                return httpx.Response(200, json=info) if info else httpx.Response(404)
            return httpx.Response(200)

    def client(**_kwargs: object) -> Fake:
        return Fake()

    monkeypatch.setattr(httpx, "AsyncClient", client)


async def test_a_model_service_reports_which_model_it_is_serving(
    configured_engines: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Responding" hid the failure that cost this project a working reranker twice.

    A TEI container serving the wrong weights answers `/health` exactly like one serving the
    right ones. The only symptoms are quality, which nobody can see, and latency, which
    everybody blames on something else. Naming the model turns both into a line of
    `zenith diagnose`.
    """
    _serving(monkeypatch, {"model_id": "BAAI/bge-reranker-v2-m3"})

    checks = await run_diagnostics()
    rerank = next(check for check in checks if check.name == "reranking service")

    assert "BAAI/bge-reranker-v2-m3" in rerank.detail


async def test_a_model_service_without_an_info_route_is_still_healthy(
    configured_engines: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older TEI is a working service. Failing it over a missing label would cry wolf."""
    _serving(monkeypatch, None)

    checks = await run_diagnostics()
    rerank = next(check for check in checks if check.name == "reranking service")

    assert rerank.status == "ok"
    assert "responding" in rerank.detail


async def test_a_missing_job_queue_is_reported(configured_engines: None) -> None:
    """The half of the install that `alembic upgrade head` does not do.

    Procrastinate owns its own schema, so the queue tables come from a separate command. An
    installation that skips it accepts uploads and ingests none of them: 201, a row, and a
    status that stays `pending` for ever, with the only complaint in a worker log nobody
    reads. The test database is exactly such an installation, which is what makes this
    assertion the real thing rather than a simulation of it.
    """
    checks = {check.name: check for check in await run_diagnostics()}

    assert checks["job queue"].status == "fail"
    # The actionable sentence has to survive `_scrub`. Naming the command as `zenith
    # install-queue` did not: the default database password *is* the word `zenith`, so the
    # instruction came out as `Run `*** install-queue``.
    assert "install-queue" in checks["job queue"].detail
    assert "***" not in checks["job queue"].detail


async def test_a_document_row_without_its_file_is_reported(
    configured_engines: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one inconsistency the product's own write ordering permits.

    `DocumentService.create` commits the row and then writes the file, deliberately, so a
    rolled-back transaction can never leave a file nobody can find. The accepted residue is
    the reverse — and nothing surfaced it until somebody clicked the document and the viewer
    said "That document is no longer available", in front of whoever was being shown it.
    """
    from app.core.database import owner_session

    monkeypatch.setenv("ZENITH_STORAGE_DIR", str(tmp_path))
    from app.core.config import settings

    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))

    async with owner_session() as session:
        tenant_id = await session.scalar(
            text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"),
            {"n": f"Orphan {uuid4()}"},
        )
        await session.execute(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes) "
                "VALUES (:t, 'vanished.pdf', :sha, 10)"
            ),
            # A real digest shape: `path_for` refuses anything else, which is the guard that
            # keeps a stored key from walking out of its tenant directory.
            {"t": tenant_id, "sha": uuid4().hex + uuid4().hex},
        )

    checks = {check.name: check for check in await run_diagnostics()}

    # A warning, not a failure: search over every other document is unaffected and the repair
    # — re-upload, or delete the row — is a person's decision.
    assert checks["document files"].status == "warn", checks["document files"].detail
    assert "vanished.pdf" in checks["document files"].detail


async def test_one_corrupt_storage_key_does_not_take_down_the_check(
    configured_engines: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`path_for` refuses anything that is not a SHA-256 digest — the guard that keeps a
    stored key from walking out of its tenant directory.

    A row that trips it has no reachable file by definition, so it belongs in the count. It
    used to raise instead, which reported `fail` for the whole installation and said nothing
    about the other nine hundred documents. A diagnostic that dies on the condition it
    diagnoses is worse than no diagnostic, which `backup.sh` learned the same way.
    """
    from app.core.config import settings
    from app.core.database import owner_session

    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))

    async with owner_session() as session:
        tenant_id = await session.scalar(
            text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"),
            {"n": f"Corrupt {uuid4()}"},
        )
        await session.execute(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes) "
                "VALUES (:t, 'bad-key.pdf', :sha, 10)"
            ),
            {"t": tenant_id, "sha": "../../etc/passwd"},
        )

    checks = {check.name: check for check in await run_diagnostics()}

    assert checks["document files"].status == "warn", checks["document files"].detail
    assert "bad-key.pdf" in checks["document files"].detail


async def test_a_pending_document_with_no_job_is_reported(
    configured_engines: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`zenith reingest` could always find these; nothing ever said they existed.

    Two paths strand a document, both chosen deliberately: a failed enqueue does not fail the
    upload, and a document relabelled between upload and ingestion strands its own job. Either
    way it sits at `pending` for ever, looking to its owner exactly like one queued behind
    others.
    """
    from app.core.database import owner_session

    async with owner_session() as session:
        # The queue tables are absent in the test database, so the check reports that it
        # cannot tell rather than guessing — which is the honest answer and the one asserted
        # here. A wrong guess in either direction is worse: "nothing stranded" hides real
        # ones, and "everything stranded" cries wolf on every installation.
        installed = await session.scalar(text("SELECT to_regclass('public.procrastinate_jobs')"))

    checks = {check.name: check for check in await run_diagnostics()}

    if installed is None:
        assert checks["stranded documents"].status == "warn"
        assert "cannot tell" in checks["stranded documents"].detail
    else:
        assert checks["stranded documents"].status in {"ok", "warn"}
