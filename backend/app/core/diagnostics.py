"""What the operator can send us when something is wrong.

We sell on-premise. When a customer says "it's slow" or "it won't start", nobody on this
team can SSH in and look — so the only diagnosis available is whatever they can run and
paste into an email. That makes this file a support channel, and it has two properties it
cannot lose.

**It must never print a secret.** Its output goes into tickets, chat threads and email.
Connection strings carry passwords, so every URL is redacted before it is shown.

**It must work when things are broken.** A diagnostic that raises when the database is
unreachable is useless at exactly the moment it is needed. Every check catches its own
failure and reports it as a result; the run always completes.
"""

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.core.database import get_owner_session_factory, get_session_factory

Status = Literal["ok", "warn", "fail"]

# Matches the password between `://user:` and the `@host`.
#
# `[^/]*` is greedy on purpose, so it runs to the **last** `@` before the path. The obvious
# `[^@]*` stops at the first one, which silently leaks everything after it when the
# password itself contains `@` — and `@` is a perfectly ordinary character in a generated
# password. A test covers exactly that case, because it is the kind of bug whose only
# symptom is a credential in someone's inbox.
_PASSWORD = re.compile(r"(://[^:/@]+:)[^/]*(@)")


def redact(url: str) -> str:
    """Hide the password in a connection string, keep everything else.

    The user, host, port and database name are exactly what someone needs to see to spot a
    misconfiguration. The password is exactly what must not reach a support ticket.
    """
    return _PASSWORD.sub(r"\1***\2", url)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: Status
    detail: str
    elapsed_ms: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


def _known_secrets() -> list[str]:
    """Every secret value this process holds, longest first.

    Longest first because a short secret that is a substring of a longer one must not be
    replaced first and leave the remainder visible.
    """
    values = [settings.jwt_secret, settings.encryption_key]
    for url in (settings.database_url, settings.database_owner_url):
        match = re.search(r"://[^:/@]+:([^/]*)@", url)
        if not match or not match.group(1):
            continue
        credential = match.group(1)
        values.append(credential)
        # A password containing a literal `@` should have been percent-encoded, and when
        # it is not, the URL is genuinely ambiguous: psycopg read
        # `u:s3cr3t@p4ss@localhost` as password `s3cr3t` and host `p4ss@localhost`, while
        # the pattern above reads the password as `s3cr3t@p4ss`. Neither is wrong, and an
        # exact match on one parse cannot scrub what the other echoes. So every fragment
        # is scrubbed, which covers both readings and any third one a driver invents.
        values.extend(credential.split("@"))
    return sorted({value for value in values if value}, key=len, reverse=True)


def _scrub(detail: str) -> str:
    """Last line of defence before anything is printed.

    `redact` handles connection strings we format ourselves. This handles the ones we do
    not control: a driver that fails to connect quotes fragments of the URL back at us,
    and that text goes straight into the report. Observed in practice — psycopg reported
    `failed to resolve host 'p4ss@localhost'` for a password containing `@`.

    So known secret values are removed by exact match, wherever they appear and whoever
    put them there. Also collapsed to one line: a multi-line traceback turns a readable
    report into a wall of text, and an operator skims this.
    """
    for secret in _known_secrets():
        detail = detail.replace(secret, "***")
    detail = " ".join(detail.split())
    return detail if len(detail) <= 200 else detail[:197] + "..."


async def _timed(name: str, work: Callable[[], Awaitable[tuple[Status, str]]]) -> Check:
    started = time.perf_counter()
    try:
        status, detail = await work()
    except Exception as exc:  # noqa: BLE001 - reporting the failure *is* the job here
        status, detail = "fail", f"{type(exc).__name__}: {exc}"
    return Check(name, status, _scrub(detail), (time.perf_counter() - started) * 1000)


async def _application_connection() -> tuple[Status, str]:
    async with get_session_factory()() as session:
        user = await session.scalar(text("SELECT current_user"))
        version = await session.scalar(text("SHOW server_version"))
    return "ok", f"connected as {user!r}, Postgres {version}"


async def _rls_active() -> tuple[Status, str]:
    """The check `verify_rls_active` runs at startup, reported rather than raised.

    Worth its own line because if this fails, isolation is off and every other number in
    this report is describing an installation that must not be serving traffic.
    """
    async with get_session_factory()() as session:
        visible = await session.scalar(text("SELECT count(*) FROM tenants"))
        user = await session.scalar(text("SELECT current_user"))
    if visible:
        return "fail", (
            f"RLS INACTIVE for {user!r}: {visible} tenants visible with no context. "
            "The application must connect as `zenith_app`, never as the schema owner."
        )
    return "ok", f"policies apply to {user!r}"


async def _migration_state() -> tuple[Status, str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    # `app/core/diagnostics.py` -> `app/core` -> `app` -> the backend root, which is where
    # `alembic.ini` lives. Resolved from this file rather than from the working directory,
    # because an operator runs `zenith diagnose` from wherever they happen to be.
    backend_root = Path(__file__).resolve().parents[2]
    script = ScriptDirectory.from_config(Config(str(backend_root / "alembic.ini")))
    expected = script.get_current_head()

    async with get_owner_session_factory()() as session:
        applied = await session.scalar(text("SELECT version_num FROM alembic_version"))

    if applied == expected:
        return "ok", f"at head ({applied})"
    return "fail", (
        f"database is at {applied}, code expects {expected}. Run `alembic upgrade head`."
    )


async def _job_queue() -> tuple[Status, str]:
    """Are the job-queue tables installed?

    Procrastinate owns its own schema and manages it itself, so it is deliberately not part
    of our migrations — mixing the two would mean our `downgrade` had opinions about a
    library's tables. The cost of that separation is a second install step, `zenith
    install-queue`, and a fresh installation that runs only `alembic upgrade head` gets an
    application which accepts uploads and never ingests one of them.

    That failure is quiet in the worst way: `POST /documents` answers 201, the row appears,
    the status stays `pending` forever, and the only complaint is in a worker log nobody is
    reading. It is exactly the shape of failure this whole module exists to make loud.
    """
    async with get_owner_session_factory()() as session:
        installed = await session.scalar(text("SELECT to_regclass('public.procrastinate_jobs')"))

    if installed is None:
        # The command is named without the `zenith` prefix on purpose. `_scrub` removes every
        # known secret from every detail, and an installation whose database password happens
        # to be the word `zenith` — which is the default in `.env.example`, and therefore in
        # every development and test environment — gets `Run \`*** install-queue\``. The one
        # actionable sentence in this whole check, redacted into nonsense exactly where it is
        # read most.
        return "fail", "job-queue tables are missing. Run the `install-queue` CLI command."

    # The owner connection, because that is the one Procrastinate itself uses — `tasks.py`
    # says why: the queue tables are ours rather than customer data, they carry no RLS, and
    # the worker has to read a job before it has any tenant context to read it with. So
    # `zenith_app` holds no privilege on them *by design*, and asking with the application
    # role reported `permission denied` on a perfectly healthy installation. A check that
    # cries wolf is a check somebody switches off.
    async with get_owner_session_factory()() as session:
        waiting = await session.scalar(
            text("SELECT count(*) FROM procrastinate_jobs WHERE status = 'todo'")
        )
    return "ok", f"installed, {waiting} job(s) waiting"


async def _orphaned_documents() -> tuple[Status, str]:
    """Rows whose PDF is no longer on disk.

    The one inconsistency the product's own ordering permits. `DocumentService.create` commits
    the row and *then* writes the file, deliberately, so a rolled-back transaction can never
    leave a file nobody can find — the accepted residue being the reverse: a row pointing at a
    file that was never written, or one lost to a restore, a migration between machines, or a
    storage directory that moved.

    Nothing surfaces it until somebody clicks the document and the viewer says *"That document
    is no longer available"* — in front of whoever is being shown the product, on a corpus that
    reports itself complete everywhere else. `backup.sh` has reported this for a while; it is
    the sort of thing an operator should not have to take a backup to discover.

    A warning rather than a failure. The installation works, search over every other document
    is unaffected, and the repair — re-upload, or delete the row — is a decision for a person.
    """
    from app.features.documents.storage import DocumentStorage

    storage = DocumentStorage()
    async with get_owner_session_factory()() as session:
        rows = (
            await session.execute(text("SELECT tenant_id, sha256, filename FROM documents"))
        ).all()

    def absent(row: Any) -> bool:
        try:
            return not storage.path_for(row.tenant_id, row.sha256).exists()
        except Exception:  # noqa: BLE001
            # `path_for` refuses anything that is not a SHA-256 digest — the guard that keeps
            # a stored key from walking out of its tenant directory. A row that trips it has
            # no reachable file by definition, so it belongs in this count; letting it raise
            # would take down the whole check over one bad row and report nothing about the
            # other nine hundred.
            return True

    missing = [row for row in rows if absent(row)]
    if not missing:
        return "ok", f"{len(rows)} document(s), every file present"

    # Named, up to a point: an operator with three broken documents wants to know which, and
    # one with three hundred wants the number and a place to start.
    shown = ", ".join(row.filename for row in missing[:3])
    more = f" and {len(missing) - 3} more" if len(missing) > 3 else ""
    return "warn", (
        f"{len(missing)} of {len(rows)} document(s) have no file on disk ({shown}{more}). "
        f"They appear in listings and fail when opened."
    )


async def _extensions() -> tuple[Status, str]:
    required = {"vector", "pg_search", "pgcrypto"}
    async with get_session_factory()() as session:
        found = set(await session.scalars(text("SELECT extname FROM pg_extension")))
    missing = required - found
    if missing:
        return "fail", f"missing: {', '.join(sorted(missing))}"
    return "ok", ", ".join(sorted(required))


async def _content() -> tuple[Status, str]:
    """Row counts through the owner connection.

    Deliberately the owner: this is an operator asking about their own installation, and
    under RLS with no context the answer would be zero for everything, which reads as data
    loss rather than as an empty context.
    """
    tables = ("tenants", "users", "documents", "chunks", "chunk_embeddings")
    counts: list[str] = []
    async with get_owner_session_factory()() as session:
        for table in tables:
            counts.append(f"{table}={await session.scalar(text(f'SELECT count(*) FROM {table}'))}")
    return "ok", "  ".join(counts)


async def _vector_space() -> tuple[Status, str]:
    async with get_owner_session_factory()() as session:
        rows = list(
            await session.execute(
                text("SELECT model, version, dimension, status FROM embedding_spaces")
            )
        )
    if not rows:
        # Expected before F6, so a warning rather than a failure: an installation that has
        # not indexed anything yet is not broken.
        return "warn", "no vector space registered yet"
    active = [f"{m}/{v} dim={d}" for m, v, d, s in rows if s == "active"]
    if not active:
        return "fail", f"{len(rows)} space(s) registered, none active — searches return nothing"
    return "ok", f"active: {', '.join(active)}"


def _model_service(name: str, url: str) -> Callable[[], Awaitable[tuple[Status, str]]]:
    """Reachable, and **serving what**.

    "Responding" was not enough. A TEI container serving a different model than the
    deployment intends looks identical to a correct one from the outside: it answers
    `/health`, it returns scores, and nothing anywhere says which weights produced them. The
    two ways that goes wrong are both real — a cross-encoder swapped for a faster one is a
    quality change nobody can see, and one swapped for a heavier one is the difference
    between a search that takes 800 ms and one that takes fourteen seconds.

    Reported rather than checked against an expected value: the model is a deployment
    decision, and this file's job is to make decisions visible, not to have opinions about
    them.
    """

    async def check() -> tuple[Status, str]:
        base = url.rstrip("/")
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{base}/health")
            if response.status_code != 200:
                return "fail", f"{redact(url)} returned {response.status_code}"
            # Best effort. An older TEI without `/info` is still a working service, and
            # failing the check over a missing label would cry wolf.
            served = ""
            try:
                info = await client.get(f"{base}/info")
                if info.status_code == 200:
                    served = str(info.json().get("model_id") or "")
            except Exception:  # noqa: BLE001 - the health answer is what decides the status
                served = ""
        return "ok", f"{redact(url)} responding{f', serving {served}' if served else ''}"

    check.__name__ = name
    return check


async def _hardware() -> tuple[Status, str]:
    """The active profile, and everything it turned off.

    Degradations must be visible. `low-spec` runs without the reranker, and M0 measured
    that at up to 20 points of Recall@8 — a customer should learn that from a diagnostic
    they can run, not by inferring it from answers that are quietly worse.
    """
    from app.core.hardware import UNMEASURED, active

    profile = active()
    detail = f"profile {profile.name!r}, embed batch budget {profile.max_batch_tokens} tokens"
    if profile.name in UNMEASURED:
        detail += " (these values are inherited from upstream defaults, not measured here)"
    if not profile.disabled:
        return "ok", detail
    # A warning, not a failure: the installation works, and it works less well. Reporting
    # it as `ok` would hide the trade; reporting it as `fail` would cry wolf on a profile
    # somebody chose on purpose.
    return "warn", f"{detail} — disabled: {'; '.join(profile.disabled)}"


async def _storage() -> tuple[Status, str]:
    """Can we write documents, and is there room for them?

    Both halves matter to an operator. A directory that is not writable turns every upload
    into a 500 with no clue in it. Running out of space is the slower failure: M0 measured
    roughly 5 MB of derived data per 100 pages *on top of* the original file, so a corpus
    the customer thinks of as "a few gigabytes of PDFs" needs meaningfully more than that.
    """
    from app.features.documents.storage import DocumentStorage

    storage = DocumentStorage()
    free_gb = await storage.free_bytes() / 1_073_741_824

    probe = storage.root / ".zenith-write-probe"
    try:
        storage.root.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"")
    except OSError as exc:
        return "fail", f"{storage.root} is not writable: {exc.strerror}"
    finally:
        probe.unlink(missing_ok=True)

    # A number rather than a threshold: how much is enough depends on a corpus size only
    # the customer knows, and a warning calibrated on a guess is a warning people learn to
    # ignore.
    return "ok", f"{storage.root} writable, {free_gb:.1f} GB free"


async def _configuration() -> tuple[Status, str]:
    """Configuration, with every secret withheld.

    The JWT secret is not printed, not even truncated: a prefix narrows a brute force, and
    there is no diagnostic value in seeing part of it. Whether it is *set* is already
    guaranteed — the process could not have started otherwise.
    """
    return "ok", (
        f"app_db={redact(settings.database_url)}  "
        f"owner_db={redact(settings.database_owner_url)}  "
        f"access_token={settings.access_token_minutes}min  "
        f"refresh_token={settings.refresh_token_days}d  "
        f"storage={settings.storage_dir}  "
        f"pool={settings.api_pool_size}/{settings.worker_pool_size}  "
        f"statement_timeout={settings.statement_timeout_ms}ms"
    )


async def run_diagnostics() -> list[Check]:
    """Every check, in dependency order, none of them able to abort the run."""
    return [
        await _timed("configuration", _configuration),
        await _timed("database (application role)", _application_connection),
        await _timed("row-level security", _rls_active),
        await _timed("migrations", _migration_state),
        # Right after migrations, because it is the half of the install that `alembic upgrade
        # head` does not do and that nothing else would report as missing.
        await _timed("job queue", _job_queue),
        await _timed("extensions", _extensions),
        await _timed("content", _content),
        await _timed("document storage", _storage),
        # After storage, because it needs the storage root to be readable to mean anything.
        await _timed("document files", _orphaned_documents),
        await _timed("hardware profile", _hardware),
        await _timed("vector space", _vector_space),
        await _timed("embedding service", _model_service("embed", settings.tei_embed_url)),
        await _timed("reranking service", _model_service("rerank", settings.tei_rerank_url)),
    ]
