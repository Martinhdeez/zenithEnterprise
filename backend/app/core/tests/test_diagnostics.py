"""Two properties this tool cannot lose, and one it is easy to think it has.

Its output goes into support tickets, so it must never print a secret. And it runs when
the installation is broken, so a check that raises instead of reporting is a diagnostic
that is absent exactly when it is needed.
"""

import json
import re
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from app.cli import app
from app.core import diagnostics
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

    assert len(checks) == 17
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


@pytest.mark.asyncio
async def test_a_clean_installation_reports_its_bypass_surface(configured_engines: None) -> None:
    """The test database is built from the migrations and nothing else, so it is the case
    the check has to call clean — anything else is a diagnostic that cries wolf on every
    installation and gets switched off."""
    checks = {check.name: check for check in await run_diagnostics()}

    assert checks["bypass surface"].status == "ok", checks["bypass surface"].detail


# --- the lock budget ---------------------------------------------------------------------

#: The three numbers the lock-budget detail opens with, read back out of it.
#:
#: Read from the message rather than by asking the database the same question a second time,
#: and that is the point rather than a convenience: a helper that recounted the partitions
#: itself would agree with a check that had stopped counting. What an operator acts on is this
#: sentence, so this sentence is what is asserted.
_SHAPE = re.compile(r"(\d+) partition\(s\) of (\d+) table\(s\) = (\d+) locks/txn")


def _partition_shape(detail: str) -> tuple[int, int, int]:
    """`(partitions, partitioned tables, relations)`, or a failure naming what it was given.

    An unparseable detail is a failure rather than a zero. The `no partitioned tables` branch
    produces one, and reading it as `(0, 0, 0)` would let this file's other tests pass against
    a check that had quietly stopped finding anything.
    """
    match = _SHAPE.search(detail)
    assert match, f"the lock budget did not report a partition shape: {detail!r}"
    return int(match[1]), int(match[2]), int(match[3])


@pytest.mark.asyncio
async def test_an_unpartitioned_installation_has_nothing_to_size_for(
    configured_engines: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On an installation with nothing partitioned, the setting is not load-bearing — and the
    report has to say that rather than pass over it.

    The distinction this exists for is unchanged: an operator reading the report must be able
    to tell "there is nothing to size for" from "nobody looked". What changed is the
    installation. Until 0026 the test database *was* unpartitioned, so this ran against the
    default and asserted the branch by accident of the schema. Since 0026 `public` holds two
    partitioned tables at `ZENITH_PARTITION_MODULUS` buckets each, and the way to keep
    asserting the same branch was either to loosen it into
    something a partitioned schema also satisfies — which would test nothing — or to give it a
    schema that is genuinely empty. This gives it one.

    An empty schema rather than a stub: the count comes back as zero from a real query against
    a real database, which is exactly what an installation that has not run 0026 produces. Not
    a hypothetical installation either — on-premise customers upgrade when they schedule it,
    so this branch is what `zenith diagnose` prints on every one of them until they do.
    """
    from app.core.database import get_owner_session_factory

    async with get_owner_session_factory()() as session:
        await session.execute(text("CREATE SCHEMA nothing_partitioned"))
        await session.commit()

    try:
        monkeypatch.setattr(diagnostics, "PARTITION_SCHEMA", "nothing_partitioned")
        checks = {check.name: check for check in await run_diagnostics()}

        detail = checks["lock budget"].detail
        assert checks["lock budget"].status == "ok", detail
        assert "no partitioned tables" in detail, detail
        # The setting and the slot count are still reported. Without them the message says
        # only that the check declined to answer, which is the half an operator cannot tell
        # from a check that did not run.
        assert "max_locks_per_transaction=" in detail, detail
        assert "slots" in detail, detail
    finally:
        async with get_owner_session_factory()() as session:
            await session.execute(text("DROP SCHEMA IF EXISTS nothing_partitioned CASCADE"))
            await session.commit()


@pytest.mark.asyncio
async def test_partitions_are_counted_out_of_the_live_schema(
    configured_engines: None,
) -> None:
    """The half that would rot if it were a constant.

    A partitioned table is built in `public`, with an index on each partition because the
    locks are taken per *relation* and an index is one — `eval/lock-budget.json` measures the
    slope at 9.00 locks per partition-pair against a schema declaring nine relations, which is
    the equality this check depends on. The detail must then name the partitions it found.

    **Asserted as a difference, because the probe is no longer alone in `public`.** Until 0026
    it was, so the totals the check reported *were* the probe's and could be matched against a
    literal. Since 0026 the schema carries a bucket per modulus of its own and that literal is
    wrong — not because the check drifted, but because the check deliberately sums over every
    partitioned table in the schema rather than over the ones a search happens to touch. So
    the run is taken twice and the probe's contribution is what is asserted, which is the
    quantity the old literal was standing in for. Matching the new total instead would have
    been a number that rots on the next migration to add a partitioned table, and this test is
    named for refusing exactly that.

    The baseline is asserted too, and it is the stronger half: reading 0026's own partitions
    of two tables before the probe exists is what proves the count comes out of the live
    schema. A
    check hard-coded to zero, or one that had stopped looking, would still pass a
    difference-only assertion.

    Dropped in a `finally`: this runs against the shared testcontainers database, and a
    partitioned table left in `public` would be picked up by `test_partition_rls_guard.py`
    as a partition carrying no policy.
    """
    from app.core.database import get_owner_session_factory

    baseline = {check.name: check for check in await run_diagnostics()}
    before = _partition_shape(baseline["lock budget"].detail)
    # `chunks` and `chunk_embeddings`, at whatever `ZENITH_PARTITION_MODULUS` the migration
    # ran with, carrying nine relations per partition-pair. Named rather than tolerated: the
    # point of the check is that it reads the schema, and a baseline of zero here would mean
    # it had stopped.
    #
    # This *is* the constant the check itself refuses to be, and that is the right way round.
    # The check computes the number so that it is never wrong; this asserts it so that it is
    # never changed silently.
    #
    # **Derived from the setting rather than written down, because the modulus stopped being a
    # constant.** `9P + 9` is not arithmetic invented here: `eval/lock-budget.json` measures
    # the slope at 9.00 locks per partition-pair and `eval/modulus-cost.json` records
    # `locks_for_request` as exactly `9P + 9` at all five of its partitioned rungs. So a
    # migration that adds one index to either table still turns this red — the relation count
    # moves off `9P + 9` and the measured claim in `_PARTITION_RELATIONS`' comment stops being
    # true at the same moment — while running the suite at a different modulus does not.
    modulus = settings.partition_modulus
    assert before == (2 * modulus, 2, 9 * modulus + 9), (before, modulus)

    async with get_owner_session_factory()() as session:
        await session.execute(
            text("CREATE TABLE lock_probe (tenant_id uuid NOT NULL) PARTITION BY HASH (tenant_id)")
        )
        await session.execute(text("CREATE INDEX ON lock_probe (tenant_id)"))
        for remainder in range(4):
            await session.execute(
                text(
                    f"CREATE TABLE lock_probe_{remainder} PARTITION OF lock_probe "
                    f"FOR VALUES WITH (MODULUS 4, REMAINDER {remainder})"
                )
            )
        await session.commit()

    try:
        checks = {check.name: check for check in await run_diagnostics()}
        detail = checks["lock budget"].detail
        after = _partition_shape(detail)
        # Four partitions, one more partitioned table, and ten more relations: the four
        # partitions and their four indexes, plus the parent and its partitioned index.
        assert (
            after[0] - before[0],
            after[1] - before[1],
            after[2] - before[2],
        ) == (4, 1, 10), f"{before} -> {after}"
        assert checks["lock budget"].status == "ok", detail
    finally:
        async with get_owner_session_factory()() as session:
            await session.execute(text("DROP TABLE IF EXISTS lock_probe CASCADE"))
            await session.commit()


@pytest.mark.asyncio
async def test_a_lock_budget_the_pools_can_exhaust_is_a_failure(
    configured_engines: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And it has to be able to fail, or it is not a check.

    The pool sizes are raised rather than the partition count, and that is not a shortcut: the
    inequality has `partitions x relations x concurrency` on one side, so either term moves it,
    and driving it with the pools tests the arithmetic without creating the several hundred
    tables it would take to break a 6,400-slot table from the other direction.

    It is also the realistic failure. `eval/lock-budget.json` brackets the boundary at 256
    partitions on the default 64: eight concurrent searches clean, twelve not. Nothing about
    the schema changed between those two rungs — only how many people were asking at once.
    """
    from app.core.database import get_owner_session_factory

    async with get_owner_session_factory()() as session:
        await session.execute(
            text("CREATE TABLE lock_probe (tenant_id uuid NOT NULL) PARTITION BY HASH (tenant_id)")
        )
        for remainder in range(4):
            await session.execute(
                text(
                    f"CREATE TABLE lock_probe_{remainder} PARTITION OF lock_probe "
                    f"FOR VALUES WITH (MODULUS 4, REMAINDER {remainder})"
                )
            )
        await session.commit()

    try:
        monkeypatch.setattr(settings, "api_pool_size", 100_000)
        checks = {check.name: check for check in await run_diagnostics()}
        detail = checks["lock budget"].detail

        assert checks["lock budget"].status == "fail", detail
        # The number to set, not just the news that it is wrong. An operator reading this in
        # a ticket has one action available and it needs a restart.
        assert "Set max_locks_per_transaction=" in detail
        assert "restart" in detail
    finally:
        async with get_owner_session_factory()() as session:
            await session.execute(text("DROP TABLE IF EXISTS lock_probe CASCADE"))
            await session.commit()


@pytest.mark.asyncio
async def test_a_security_definer_function_nobody_declared_is_reported(
    configured_engines: None,
) -> None:
    """The finding `test_security_definer_audit.py` structurally cannot make.

    That test audits a container built from the migrations. This audits the installation, and
    the gap between them is not hypothetical: two `SECURITY DEFINER` functions left over from
    the F18 BM25 investigation were found on a live database, in no migration, no file and no
    branch. Their tables had since been dropped so nothing could call them — but nothing in
    the repository knew they existed, which is the part that had to stop being true.

    Created and dropped here rather than fixtured: the container is shared for the whole
    session, and a leftover would fail the declared-schema audit in a different file with a
    finding this test planted.
    """
    from app.core.database import owner_session

    async with owner_session() as session:
        await session.execute(
            text(
                "CREATE FUNCTION zenith_left_behind() RETURNS integer "
                "LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp "
                "AS $$ SELECT 1 $$"
            )
        )
    try:
        checks = {check.name: check for check in await run_diagnostics()}
    finally:
        async with owner_session() as session:
            await session.execute(text("DROP FUNCTION zenith_left_behind()"))

    surface = checks["bypass surface"]
    # A failure rather than a warning, because nothing revoked the default `EXECUTE` from
    # `PUBLIC` — which is exactly how the two real ones were left, and is what decides whether
    # an undeclared function is reachable by anything holding a credential on the database.
    assert surface.status == "fail", surface.detail
    assert "PUBLIC EXECUTE" in surface.detail
    # The owner, because "who does this run as" is the question that makes it a bypass at all.
    assert "owner " in surface.detail
    # `_left_behind()` rather than the whole name, and the reason is the trap `_job_queue`
    # already documents from the other side: `_scrub` removes every known secret by exact
    # match, and the default database password in `.env.example` is the word `zenith` — which
    # is the prefix of every identifier this schema owns. So on an installation that kept the
    # default, this check reports `***_left_behind()`. The count, the owner and the reach
    # survive, which is enough to decide whether to act; the name does not. Asserting the full
    # name here would only be asserting that this test environment has a different password.
    assert "_left_behind()" in surface.detail


@pytest.mark.asyncio
async def test_a_declared_function_missing_from_the_installation_is_reported(
    configured_engines: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction, and the one that says somebody has been editing a live schema.

    A list that claims a bypass which is not there is wrong in the way that matters: it is
    the list the next reviewer trusts instead of reading the database.

    Done by adding a name to the list rather than by dropping a real function. Dropping one
    would take its trigger with it, and restoring it here would leave the shared container
    holding this file's idea of the function instead of migration 0003's — the comparison is
    what is under test, not Postgres's `DROP`.
    """
    monkeypatch.setattr(
        diagnostics,
        "AUTHORISED_SECURITY_DEFINERS",
        diagnostics.AUTHORISED_SECURITY_DEFINERS | {"zenith_never_created()"},
    )

    checks = {check.name: check for check in await run_diagnostics()}

    surface = checks["bypass surface"]
    assert surface.status == "fail", surface.detail
    assert "declared but absent" in surface.detail
    # Truncated for the redaction reason given in the test above.
    assert "_never_created()" in surface.detail


# --- the reranker alarm ------------------------------------------------------------------
#
# 28 August: `tei-rerank` was killed for memory and nothing said so. Search kept answering
# from the fused order, about fifteen points of recall worse, marked `degraded` in a field
# nobody was reading, and it was found by accident hours later. The compose file now caps the
# reranker's CPUs and restarts it; these are the tests for the half that was still missing,
# which is anybody being told.


def _reranker(
    monkeypatch: pytest.MonkeyPatch, *, health: int = 200, metrics: str | None = None
) -> None:
    """Stand in for the reranker container: a `/health` code, and `/metrics` or nothing."""
    import httpx

    class Fake:
        async def __aenter__(self) -> "Fake":
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def get(self, url: str) -> httpx.Response:
            if url.endswith("/metrics"):
                # `None` is a TEI with no `/metrics` route; `""` is one that has it and has
                # served nothing. The check has to tell those apart, so the fake does too.
                if metrics is None:
                    return httpx.Response(404)
                return httpx.Response(200, text=metrics)
            if url.endswith("/info"):
                return httpx.Response(404)
            return httpx.Response(health)

    def client(**_kwargs: object) -> Fake:
        return Fake()

    monkeypatch.setattr(httpx, "AsyncClient", client)


@pytest.mark.asyncio
async def test_an_unreachable_reranker_fails_rather_than_warns(
    configured_engines: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The severity is the point of the whole check.

    A warning is what the product already did — it degraded, said so in a response field, and
    carried on. Nobody read it. The runbook and `demo-check.sh` both call a missing reranker a
    failure because an installation that answers without it shows a recall number nobody can
    reproduce, and this has to say the same thing or it is a third opinion.
    """
    monkeypatch.setattr(settings, "tei_rerank_url", "http://127.0.0.1:1")

    checks = {check.name: check for check in await run_diagnostics()}

    reranker = checks["reranker health"]
    assert reranker.status == "fail", reranker.detail
    # Named so an operator knows which container to start, and what they are losing until
    # they do. Both halves have to survive `_scrub`; the test below is what holds them to it.
    assert "tei-rerank" in reranker.detail
    assert "fused order" in reranker.detail


@pytest.mark.asyncio
async def test_a_reranker_that_answers_health_with_an_error_fails(
    configured_engines: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TEI answers 503 while it is loading a model, which is exactly the window a restart
    loop spends most of its time in. Up is not the same as serving."""
    _reranker(monkeypatch, health=503)

    checks = {check.name: check for check in await run_diagnostics()}

    assert checks["reranker health"].status == "fail"
    assert "503" in checks["reranker health"].detail


@pytest.mark.asyncio
async def test_a_reranker_that_has_served_nothing_since_it_started_is_reported(
    configured_engines: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restart signature, and the only one reachable from inside a container.

    Docker's `.RestartCount` is on the host. What is reachable here is TEI's own counters,
    which live in the serving process and start again at zero when it does — so a reranker
    that is up and has answered no inference request since it started is a process younger
    than the traffic it exists to serve. A check that only asked "is it up" would call the
    OOM loop healthy between kills, which is the failure this exists for.

    A warning rather than a failure, because the proxy cannot tell that apart from a fresh
    installation nobody has searched yet. `demo-check.sh` reads the exact count instead.
    """
    _reranker(monkeypatch, metrics="# TYPE te_request_count counter\nte_request_count 0\n")

    checks = {check.name: check for check in await run_diagnostics()}

    reranker = checks["reranker health"]
    assert reranker.status == "warn", reranker.detail
    assert "0 requests since it last started" in reranker.detail
    # Honest about the limit, in the report itself and not only in a docstring.
    assert "docker inspect" in reranker.detail


@pytest.mark.asyncio
async def test_an_empty_metrics_body_is_zero_and_not_silence(
    configured_engines: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a real reranker three seconds out of a kill actually looks like.

    This was written expecting `te_request_count 0`, and the container disagreed: a freshly
    started TEI answers `/metrics` with `200` and an empty body, because a Prometheus counter
    that has never been incremented is not rendered at all. The first version read that as
    "no metrics, cannot tell" and reported `ok` — a check that called the OOM window healthy,
    on the one installation where it had just been proved otherwise.

    So the status code decides whether the question could be asked, and the body decides the
    answer. `404` is a TEI without the route; `200` with nothing in it is zero.
    """
    _reranker(monkeypatch, metrics="")

    checks = {check.name: check for check in await run_diagnostics()}

    reranker = checks["reranker health"]
    assert reranker.status == "warn", reranker.detail
    assert "0 requests since it last started" in reranker.detail


@pytest.mark.asyncio
async def test_a_working_reranker_reports_the_work_it_has_done(
    configured_engines: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Summed across TEI's label sets. Which `method` series exist is a detail of how the
    client batched, not of how much the service has served."""
    _reranker(
        monkeypatch,
        metrics=(
            "# TYPE te_request_count counter\n"
            'te_request_count{method="batch"} 1600\n'
            'te_request_count{method="single"} 33\n'
        ),
    )

    checks = {check.name: check for check in await run_diagnostics()}

    reranker = checks["reranker health"]
    assert reranker.status == "ok", reranker.detail
    assert "1633 request(s)" in reranker.detail


@pytest.mark.asyncio
async def test_a_reranker_without_metrics_is_still_healthy(
    configured_engines: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older TEI is a working reranker. Failing over a missing counter would cry wolf, and
    a check that cries wolf is a check somebody switches off — so it says what it cannot see
    instead of guessing in either direction."""
    _reranker(monkeypatch, metrics=None)

    checks = {check.name: check for check in await run_diagnostics()}

    assert checks["reranker health"].status == "ok"
    assert "invisible from here" in checks["reranker health"].detail


@pytest.mark.asyncio
async def test_the_reranker_alarm_survives_the_default_database_password(
    configured_engines: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trap `_job_queue` and the bypass surface both walked into first.

    `_scrub` removes every known secret by exact match, and the default database password in
    `.env.example` is the word `zenith` — the prefix of every identifier this schema owns. So
    on a default-password installation `zenith-tei-rerank-1` prints as `***-tei-rerank-1` and
    `Run \\`zenith diagnose\\`` prints as `Run \\`*** diagnose\\``. The sentence an operator acts
    on is the one that must not be the casualty, so this check names the *service* and never
    the container.
    """
    monkeypatch.setattr(
        settings, "database_owner_url", "postgresql+psycopg://zenith:zenith@db:5432/zenith"
    )
    monkeypatch.setattr(settings, "tei_rerank_url", "http://127.0.0.1:1")

    checks = {check.name: check for check in await run_diagnostics()}

    reranker = checks["reranker health"]
    assert "***" not in reranker.detail, reranker.detail
    assert "tei-rerank" in reranker.detail


@pytest.mark.asyncio
async def test_an_open_circuit_is_reported_and_a_closed_one_is_not(
    configured_engines: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """How long it has been degraded comes from `breaker.py`, which already tracks it.

    Only the open state is printed. A closed breaker in this process means "nothing here has
    called the reranker" — `zenith diagnose` is not the uvicorn process, and the breaker is
    per-process and in memory by design — so printing "circuit closed" would be reporting
    health that nothing observed. `demo-check.sh` records what that costs: a check that
    reports health when it cannot tell is worse than one that cries wolf, because nobody
    switches it off and nobody looks again.
    """
    from app.features.retrieval import service as retrieval_service
    from app.features.retrieval.breaker import Breaker

    _reranker(monkeypatch, metrics='te_request_count{method="batch"} 12\n')

    healthy = {check.name: check for check in await run_diagnostics()}
    assert "ircuit" not in healthy["reranker health"].detail

    tripped = Breaker(_now=lambda: 0.0)
    for _ in range(tripped.failures_to_open):
        tripped.failed()
    monkeypatch.setattr(retrieval_service, "RERANKER_BREAKER", tripped)

    degraded = {check.name: check for check in await run_diagnostics()}
    assert "Circuit open" in degraded["reranker health"].detail
