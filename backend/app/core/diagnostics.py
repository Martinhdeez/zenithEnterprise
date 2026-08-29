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
from typing import Any, Final, Literal

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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


# --- The `SECURITY DEFINER` surface ---------------------------------------------------
#
# The third class of RLS bypass, and the one no grep finds: such a function executes as its
# owner, so the policies are not applied to it, and nothing in Python names it. See CLAUDE.md
# invariant 2.
#
# The list lives here rather than in a test because it has two readers.
# `tests/integration/test_security_definer_audit.py` holds the schema the *migrations
# declare* to it; `_security_definer_surface` below holds a *running installation* to it.
# Those are different questions — the same distinction CLAUDE.md draws between `make check`
# and `demo-check` — and answering them from two copies of the list would be two catalogues
# of justified bypasses drifting apart. `eval/harness.py` makes that argument about a credit
# rule, where the cost is an incomparable report; here the cost is a bypass nobody lists.


#: Every `SECURITY DEFINER` function the schema is allowed to contain, by identity
#: signature. An overload is a different function and needs its own entry.
#:
#: An entry is added for a security guarantee, never for ergonomics — ADR 0001's rule, and
#: `.artifacts/todo/2026-08-02-f5-ingestion.md` records a route declined on exactly it.
AUTHORISED_SECURITY_DEFINERS: frozenset[str] = frozenset(
    {
        # 0002, replaced in place by 0010 — login has to find a user before a tenant context
        # exists, because the context is what the login is establishing. Returns four fields
        # for one address; the alternative was an owner session in an unauthenticated route.
        "zenith_authenticate_lookup(p_email text)",
        # 0003 — maintains `documents.label_ids` from `document_labels`. Bypasses so that an
        # administrator with `labels.manage` can remove a label they do not personally reach
        # without the `WITH CHECK` on `documents` rejecting a row they never mentioned.
        "zenith_sync_document_labels()",
        # 0003 — propagates that same array down to `chunks`, for the same reason. Rewritten
        # in place by 0026 to add `AND tenant_id = NEW.tenant_id`: `chunks` is partitioned,
        # an UPDATE picks its result relations at plan time, and without a constant for the
        # partition key it opens all 256 for writing. The predicate is redundant — a chunk's
        # tenant is its document's, enforced by `fk_chunks_document_id` — and changes no row.
        # The bypass is unchanged and so is the signature.
        "zenith_sync_chunk_labels()",
        # 0003 — gives a chunk its document's labels at insert time; without it a chunk is
        # born unlabelled, which in this schema means readable by the whole tenant.
        "zenith_fill_chunk_labels()",
        # 0016 — an invitation or reset link is consumed by an unauthenticated route, so
        # there is no tenant for a policy to filter on. Takes a hash, returns one row.
        "zenith_credential_token_lookup(p_hash text)",
        # 0016 — spends the link and sets the password in one statement, so there is no
        # window in which the link is used and no password was set.
        "zenith_credential_token_consume(p_hash text, p_password_hash text)",
        # 0022 — BM25 needs the tenant and label clauses *inside* the Tantivy query, which a
        # policy cannot express. Enforces them imperatively instead; `test_bm25_isolation.py`
        # is what makes that enforcement worth the same as a policy.
        #
        # Rewritten in place by 0026. The tenant is now read into a plpgsql local and that
        # local is used both in the Tantivy term and as an ordinary SQL qualifier, so the
        # planner has a constant to prune 256 partitions on. It is deliberately **not** a
        # parameter of the function: this bypass is only safe because no caller can name the
        # tenant, and an argument would hand that away. The signature is therefore unchanged,
        # which is also what keeps this entry accurate.
        "zenith_lexical_search(query_string text, want integer)",
    }
)

#: `public` is the only schema this project creates objects in. The ParadeDB image ships
#: several others — `paradedb`, `topology`, `tiger` — and they are not ours to vet.
#:
#: Extension-owned functions inside `public` are deliberately *not* excluded. None of them is
#: `SECURITY DEFINER` today, and the day an extension is added that ships one, adding that
#: extension has widened the bypass surface and should be argued for like anything else.
SECURITY_DEFINER_SCHEMA = "public"

# `grantee = 0` is `PUBLIC` in `pg_proc.proacl`. A NULL acl means nobody has said anything,
# and for a function the default is `EXECUTE` to `PUBLIC` — which is why the NULL case counts
# as public rather than as restricted. That default is the whole reason this column is
# reported: a bypass only `zenith_app` can call and one any role can call are different
# findings, and 0022 restricting `zenith_lexical_search` is what the difference looks like.
_SECURITY_DEFINERS = """
SELECT p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')' AS signature,
       pg_get_userbyid(p.proowner) AS owner,
       p.proacl IS NULL OR EXISTS (
           SELECT 1 FROM aclexplode(p.proacl) a
           WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE'
       ) AS public_execute,
       coalesce(p.proconfig, '{}') AS config
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.prosecdef AND n.nspname = :schema
ORDER BY signature
"""


@dataclass(frozen=True, slots=True)
class SecurityDefiner:
    """One `SECURITY DEFINER` function as the database actually holds it."""

    signature: str
    owner: str
    public_execute: bool
    config: tuple[str, ...]

    @property
    def pins_search_path(self) -> bool:
        """Without a fixed `search_path`, owner-privileged code resolves names through
        schemas the *caller* chooses. That is the standard escalation against one of these,
        and migration 0002 pinned it for that reason before anything else was written."""
        return any(setting.startswith("search_path=") for setting in self.config)


async def security_definers(session: AsyncSession) -> list[SecurityDefiner]:
    """Read the bypass surface out of the catalogue of whatever database this is.

    Takes a session rather than opening one, because the two callers ask about different
    databases: the diagnostic asks about the installation, the test asks about a container
    built from the migrations.
    """
    rows = await session.execute(text(_SECURITY_DEFINERS), {"schema": SECURITY_DEFINER_SCHEMA})
    return [
        SecurityDefiner(signature, owner, public_execute, tuple(config))
        for signature, owner, public_execute, config in rows
    ]


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


def _describe(function: SecurityDefiner) -> str:
    reach = "PUBLIC EXECUTE" if function.public_execute else "restricted"
    return f"{function.signature} (owner {function.owner}, {reach})"


async def _security_definer_surface() -> tuple[Status, str]:
    """The bypass surface of *this installation*, against the list of the justified ones.

    `test_security_definer_audit.py` already holds the migrations to that list, and that is
    the check which catches the next person to add one. It cannot catch this: it audits a
    container built from the migrations, so a function created by hand on a running database
    is invisible to it and to every grep and every branch. Two such functions were found on a
    live installation — leftovers of the F18 BM25 investigation, owned by the schema owner and
    carrying `PUBLIC EXECUTE`, declared in no migration and no file.

    Which is the same shape as the trap CLAUDE.md records two bullets apart: a green suite
    does not mean the database is migrated, and a declared schema is not the installed one.

    `PUBLIC EXECUTE` decides the severity — a failure when `PUBLIC` may execute an undeclared
    function, a warning when it may not — because an owner-privileged function every role can
    call is reachable by anything holding any credential on the database, and a restricted one
    is reachable only by whoever was granted it. It is *not* a signal that something is wrong
    by itself: it is the default for a function, four of the declared seven carry it, and
    `test_security_definer_audit.py` records which and why. A declared function that is
    *absent* fails too: at head, that means somebody has been editing the live schema by hand,
    and the next thing they leave behind may not be a harmless leftover.
    """
    async with get_session_factory()() as session:
        installed = await security_definers(session)

    undeclared = [f for f in installed if f.signature not in AUTHORISED_SECURITY_DEFINERS]
    absent = sorted(AUTHORISED_SECURITY_DEFINERS - {f.signature for f in installed})
    unpinned = sorted(f.signature for f in installed if not f.pins_search_path)

    findings: list[str] = []
    if undeclared:
        # Two, then a count. `_scrub` truncates a detail at 200 characters, and a finding cut
        # off mid-name is one nobody can act on.
        #
        # Known limitation: `_scrub` removes every known secret by exact match, so an
        # installation whose database password is the word `zenith` — the default in
        # `.env.example` — gets these names redacted into `***_lexical`. The count, the owner
        # and `PUBLIC EXECUTE` still come through, which is what decides whether to act.
        more = f" and {len(undeclared) - 2} more" if len(undeclared) > 2 else ""
        findings.append("undeclared: " + "; ".join(map(_describe, undeclared[:2])) + more)
    if absent:
        findings.append(f"declared but absent: {', '.join(absent)}")
    if unpinned:
        findings.append(f"no pinned search_path: {', '.join(unpinned)}")

    if not findings:
        return "ok", f"{len(installed)} function(s), every one declared"

    status: Status = "fail" if absent or any(f.public_execute for f in undeclared) else "warn"
    return status, "  ".join(findings)


# --- The lock budget ------------------------------------------------------------------
#
# A query over a partitioned table takes an `AccessShareLock` on every relation of every
# partition **at plan time**, before runtime pruning has removed anything, and the lock table
# it draws from is one table for the whole cluster. When it runs out, Postgres raises
# `OutOfMemory` during *planning*: the query never runs and an ordinary search is a 500.
#
# `eval/lock-budget.json` measured the shape. Locks are taken per relation, exactly — the
# slope across two rungs is 9.00 locks per partition-pair against a schema declaring nine
# relations — and the second and third statements of a search request add none, because the
# planner already opened every index of both tables for the first. So the budget is a
# property of the schema, not of the query mix, and it can be computed.
#
# It is computed here rather than written down, because a constant would be wrong the day
# the partition count or the index set changes and nothing would say so. That is the same
# reason `AUTHORISED_SECURITY_DEFINERS` is checked against a *running* installation above
# and not only against the migrations.


#: What a transaction may be asked to hold at once, over and above the application pools.
#:
#: `api_pool_size` and `worker_pool_size` both run with `max_overflow=0`, so they are hard
#: ceilings on concurrent application transactions rather than targets. The owner and
#: platform engines add two apiece (`core.database`), and they are counted because a
#: diagnostic or a `/system` page running beside a search competes for the same slots.
BYPASS_POOL_CONNECTIONS: Final = 4

#: How much headroom below which the setting is reported as a warning rather than an error.
#: A quarter, because the measured boundary is not sharp: `eval/lock-budget.json` records a
#: transaction holding 2,313 locks against a nominal 6,400-slot table with eight of them in
#: flight — 18,504 slots' worth — and succeeding, because the lock hash table grows into
#: shared memory nobody reserved. That surplus is real, transient and shared with every other
#: backend, so an installation sitting on it is not failing yet and is not safe either.
LOCK_BUDGET_HEADROOM: Final = 1.25

# Every relation a partitioned table contributes: its partitions, its partitions' indexes,
# the partitioned parents and their partitioned indexes. `relispartition` covers the first
# two; `relkind IN ('p', 'I')` covers the last two, and both are locked — measured, not
# assumed: at 256 partitions of a pair carrying nine relations the count is 2,313, which is
# 9 x 256 for the partitions plus nine for the two parents and their seven partitioned
# indexes.
#
# Summed over every partitioned table in the schema, which is the ceiling for any transaction
# rather than the cost of one particular query. Naming the tables a search touches would be
# a list to keep in step with the schema, and this file exists because those rot.

#: The schema this counts partitions in. `public` is the only one this project creates
#: objects in, exactly as `SECURITY_DEFINER_SCHEMA` above says of the bypass surface, and it
#: is a constant here for the same reason that one is: the check has two readers asking about
#: two different databases. The installation this runs against has been partitioned since
#: 0026, so `public` can no longer exhibit the unpartitioned branch, and
#: `test_an_unpartitioned_installation_has_nothing_to_size_for` points this at a schema
#: holding nothing rather than asserting that branch's message against a schema that cannot
#: produce it. Production reads the constant and is unchanged.
PARTITION_SCHEMA = "public"

_PARTITION_RELATIONS = """
SELECT count(*) AS relations,
       count(*) FILTER (WHERE c.relkind = 'p') AS partitioned_tables,
       count(*) FILTER (WHERE c.relispartition AND c.relkind = 'r') AS partitions
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = :schema
  AND (c.relispartition OR c.relkind IN ('p', 'I'))
"""


async def _lock_budget() -> tuple[Status, str]:
    """Does this installation's `max_locks_per_transaction` cover its partition count?

    Two questions, and they are the split this whole file is built on. `make check` can ask
    whether the code is right; only a running installation knows how many partitions it has
    and what its Postgres was started with, and the answer is a restart away from being
    fixed — so it has to be asked here, before somebody discovers it as a 500.

    **`max_locks_per_transaction` is not a per-transaction cap.** It sizes one table of
    `max_locks_per_transaction * (max_connections + max_prepared_transactions)` slots that
    every backend draws from, so the constraint is on concurrency and a single query passing
    proves nothing about ten. Both are reported: a setting too small for one query is a
    certain failure, and one too small for the pools is a failure under load only.

    Returns `ok` on an installation with no partitioned tables. That is not a pass by
    omission — there is genuinely nothing to size for until something is partitioned, and
    saying so is what lets an operator tell that from a check that did not run. That branch
    stopped being the development default when 0026 landed and did *not* stop mattering: any
    installation that has not yet run 0026 reaches it on every `zenith diagnose`, and the
    on-premise ones are upgraded when the customer schedules it rather than when we ship.
    """
    async with get_owner_session_factory()() as session:
        row = (
            await session.execute(text(_PARTITION_RELATIONS), {"schema": PARTITION_SCHEMA})
        ).one()
        rows = await session.execute(
            text(
                "SELECT name, setting FROM pg_settings WHERE name IN "
                "('max_locks_per_transaction', 'max_connections', "
                "'max_prepared_transactions')"
            )
        )
        server = {str(name): int(setting) for name, setting in rows}

    per_transaction = int(row.relations)
    backends = server["max_connections"] + server["max_prepared_transactions"]
    slots = server["max_locks_per_transaction"] * backends
    setting = server["max_locks_per_transaction"]

    if not per_transaction:
        return "ok", (
            f"max_locks_per_transaction={setting} x {backends} = {slots} slots; "
            "no partitioned tables, so nothing draws on them yet"
        )

    concurrency = settings.api_pool_size + settings.worker_pool_size + BYPASS_POOL_CONNECTIONS
    needed = per_transaction * concurrency
    # What the setting would have to be, phrased as the thing an operator changes. Ceiling
    # division: a fractional slot is a slot short.
    required = -(-needed // backends)
    # Short on purpose, and `fix` is kept short for the same reason `_breaker_note` is:
    # `_scrub` truncates a detail at 200 characters, and the half naming the setting to change
    # must never be the half that is cut. The first version of this said the same thing in 208
    # characters and lost the word "restart", which is the part with a consequence.
    shape = (
        f"{row.partitions} partition(s) of {row.partitioned_tables} table(s) = "
        f"{per_transaction} locks/txn; {concurrency} concurrent needs {needed} of {slots} slots"
    )
    fix = f"Set max_locks_per_transaction={required} and restart."

    if slots < per_transaction:
        return "fail", f"{shape}. One query alone exceeds the table: every search 500s. {fix}"
    if slots < needed:
        return "fail", f"{shape}. Room for {slots // per_transaction} concurrent. {fix}"
    if slots < needed * LOCK_BUDGET_HEADROOM:
        # A warning rather than a failure: it works, and the margin it is working on is
        # shared memory nobody reserved and every other backend may want.
        return "warn", f"{shape}. Under a quarter of headroom. {fix}"
    return "ok", f"{shape}, room for {slots // per_transaction} concurrent"


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


async def _stranded_documents() -> tuple[Status, str]:
    """Documents that should be in the pipeline and are not.

    Two paths leave one behind, both chosen deliberately and both documented in
    `ingestion/requeue.py`: a failed enqueue does not fail the upload, because losing a
    customer's document to a queue insert would be far worse than leaving it `pending`; and a
    document relabelled between upload and ingestion strands its own job, which is the price
    of keeping the RLS bypass surface at four routes.

    `zenith reingest` has been able to find and fix these since it was written. Nothing ever
    said they existed — the document sits at `pending` for ever, looking to its owner exactly
    like one that is merely queued behind others.

    Which is why this asks a narrower question than `find_stranded` does. Anything `pending`
    *with* a job waiting is a healthy queue doing its work; only `pending` with nothing behind
    it is stuck. A check that counted the first would report a busy installation as broken
    every time somebody uploaded a batch.
    """
    from app.features.documents.model import IN_FLIGHT

    async with get_owner_session_factory()() as session:
        if not await session.scalar(text("SELECT to_regclass('public.procrastinate_jobs')")):
            # The queue check above already reports this, and with the sentence that fixes it.
            return "warn", "cannot tell: the job-queue tables are not installed"

        # Every in-flight status, not only `pending`. A worker killed mid-document leaves it
        # at whatever stage it had reached and nothing moves it again — always true of
        # `parsing`, `chunking` and `embedding`, and one more since `classifying` (0019).
        # Derived from the model rather than listed, for the reason `IN_FLIGHT` exists.
        stranded = await session.scalar(
            text(
                "SELECT count(*) FROM documents d "
                "WHERE d.status = ANY(:statuses) AND NOT EXISTS ("
                "  SELECT 1 FROM procrastinate_jobs j "
                "  WHERE j.status IN ('todo', 'doing') "
                "    AND j.args->>'document_id' = d.id::text"
                ")"
            ),
            {"statuses": list(IN_FLIGHT)},
        )

    if not stranded:
        return "ok", "no documents waiting without a job"
    return "warn", (
        f"{stranded} document(s) are pending with no job behind them and will never ingest. "
        f"Run the `reingest` CLI command to put them back in the queue."
    )


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
            await session.execute(
                text("SELECT tenant_id, sha256, filename, media_type FROM documents")
            )
        ).all()

    def absent(row: Any) -> bool:
        try:
            return not storage.path_for(row.tenant_id, row.sha256, row.media_type).exists()
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


#: Tables small enough, and unpartitioned enough, to count exactly.
_COUNTED_EXACTLY = ("tenants", "users", "documents")

#: The two tables ADR 0009 partitions, counted from the planner's own statistics instead.
#:
#: `SELECT count(*)` over a table partitioned into 256 opens all 256 and reads all of them:
#: 1,542 locks of this installation's 6,400, measured in `eval/unpruned-queries.json`. It is
#: not slow — 4.6 ms there — and slowness was never the objection. The objection is that a
#: diagnostic an operator runs *while the installation is serving* should not take a quarter
#: of the cluster's lock table to answer a question nobody needs to the row.
#:
#: The estimate scans nothing at any modulus and takes 7 locks. It is an estimate, and the
#: `~` in the output says so rather than the reader having to know.
_ESTIMATED = ("chunks", "chunk_embeddings")

#: Summed over the partition tree, because after partitioning the parent's own `reltuples` is
#: zero and a reader of that number would conclude the corpus had been lost. Recursive rather
#: than one level down, so it still holds if a partition is ever itself partitioned.
_ESTIMATE = """
WITH RECURSIVE tree AS (
    SELECT to_regclass(:table)::oid AS oid
    UNION ALL
    SELECT i.inhrelid FROM pg_inherits i JOIN tree t ON i.inhparent = t.oid
)
SELECT coalesce(sum(c.reltuples), 0)::bigint
FROM tree JOIN pg_class c ON c.oid = tree.oid
WHERE c.relkind = 'r'
"""


async def _content() -> tuple[Status, str]:
    """Row counts through the owner connection.

    Deliberately the owner: this is an operator asking about their own installation, and
    under RLS with no context the answer would be zero for everything, which reads as data
    loss rather than as an empty context.

    The two partitioned tables are estimated rather than counted — see `_ESTIMATED`. A
    `reltuples` figure is as stale as the last `ANALYZE`, which on a busy installation is a
    real difference and on this one was zero at every rung of
    `eval/unpruned-queries.json`'s ladder. `-1` means the table has never been analysed at
    all, and it is reported as unknown rather than shown to an operator as a negative corpus.
    """
    counts: list[str] = []
    async with get_owner_session_factory()() as session:
        for table in _COUNTED_EXACTLY:
            counts.append(f"{table}={await session.scalar(text(f'SELECT count(*) FROM {table}'))}")
        for table in _ESTIMATED:
            estimate = await session.scalar(text(_ESTIMATE), {"table": table})
            counts.append(
                f"{table}={'unknown' if estimate is None or estimate < 0 else f'~{estimate}'}"
            )
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


#: What losing the reranker costs, in one sentence, said the same way in every branch below.
#:
#: Every word here has been chosen to survive `_scrub`. That is not a stylistic preference:
#: `_scrub` removes every known secret by exact match, the default database password in
#: `.env.example` is the word `zenith`, and the word `nothing` is the password in the
#: unreachable-database URL the tests point at. So this sentence names the *service*
#: (`tei-rerank`) rather than the container (`zenith-tei-rerank-1`, which prints as
#: `***-tei-rerank-1`), and counts with a digit rather than saying a component answered
#: "nothing". `_job_queue` documents the same trap from the other side.
#:
#: Short on purpose too. `_scrub` truncates at 200 characters and this is the half an
#: operator acts on, so it must never be the half that is cut.
_RERANKER_COST: Final = (
    "Search answers from the fused order, about 15 points of recall worse (F7). "
    "Start the `tei-rerank` container."
)

#: TEI's Prometheus counters are per-process and start at zero, so a restart resets them.
#: `te_request_count` moves once per inference call and not at all for `/health` or `/info`,
#: which is what makes it a measure of work done rather than of liveness.
#:
#: Label sets are summed: TEI emits one series per `method` (`batch`, `single`), and which
#: ones exist is a detail of how the client batched, not of how much the service has served.
_TEI_REQUEST_COUNT = re.compile(
    r"^te_request_count(?:\{[^}]*\})?\s+([0-9]+(?:\.[0-9]+)?)\s*$", re.MULTILINE
)


def _requests_served(metrics: str | None) -> int | None:
    """Inference requests this reranker process has answered since it started.

    `None` means the question could not be asked — no `/metrics` route at all, which an
    older TEI is entitled not to have.

    **An empty body is zero, not silence**, and the difference is the entire restart proxy.
    A freshly started TEI answers `/metrics` with `200` and *nothing in it*: a Prometheus
    counter that has never been incremented is not rendered, so the series appears only once
    the service has done some work. Reading that as "cannot tell" reported a reranker three
    seconds out of an OOM kill as healthy, which is the exact silence this check exists to
    break. The status code is what separates the two cases, so the caller passes text only
    when it got a `200`.
    """
    if metrics is None:
        return None
    return sum(int(float(value)) for value in _TEI_REQUEST_COUNT.findall(metrics))


def _breaker_note() -> str:
    """What the circuit breaker knows about how long this has been going on — and silence
    when it knows nothing, which here is almost always.

    `breaker.py` already tracks this and the answer belongs to it, so this reads it rather
    than starting a second mechanism it would have to keep in step. What it cannot do is
    read it from *another process*: the breaker is deliberately per-process and in memory
    (breaker.py says why — a shared one would mean Redis or a table to solve a problem
    measured in seconds), and `zenith diagnose` is a separate process from the uvicorn
    workers that serve search. So the breaker this function imports is a freshly
    constructed one that has never called anything.

    Which is why a closed breaker prints nothing at all. "Circuit closed" would read as
    "the installation is not degraded" — a claim this process has no way to make, and
    exactly the failure `demo-check.sh` records in its own comments: a check that reports
    health when it cannot tell is worse than one that cries wolf, because nobody switches
    it off and nobody looks again. An *open* breaker is only ever true, so that one is
    worth printing wherever it is seen.
    """
    from app.features.retrieval.breaker import State
    from app.features.retrieval.service import RERANKER_BREAKER

    breaker = RERANKER_BREAKER
    if breaker.state is State.OPEN:
        return f" Circuit open: skipped for up to the last {breaker.cooldown:.0f}s."
    if breaker.state is State.HALF_OPEN:
        return f" Circuit open for at least {breaker.cooldown:.0f}s, retrying."
    return ""


async def _reranker_health() -> tuple[Status, str]:
    """Is the reranker there, and has it just come back?

    `reranking service` above asks whether a model endpoint responds and names the weights
    it is serving, for both TEI containers alike. This asks the two questions that were
    unanswered on 28 August, when `tei-rerank` was killed for memory and *nothing said so*:
    search kept answering from the fused order, about fifteen points of recall worse,
    marked `degraded` in a field nobody was reading, and it was found by accident hours
    later.

    **A failure, never a warning.** The runbook and `demo-check.sh` both already treat a
    missing reranker as a failure, and for the same reason: an installation that answers
    without it is a working product showing a recall number nobody can reproduce. Severity
    here is set by what the absence costs, not by whether an HTTP call raised.

    **The restart proxy, and what it cannot see.** The container it is asking about is not
    this one, so Docker's restart count is out of reach — `.RestartCount` is on the host,
    which is where `demo-check.sh` reads it. What *is* reachable is TEI's own
    `/metrics`: those counters live in the serving process and start again at zero when it
    does. A reranker that is up and has answered zero requests since it started is a
    process younger than the traffic it exists to serve, which is what the OOM loop looks
    like from in here.

    It is a proxy and it is reported as one. It cannot say how many times the service
    restarted, when, or why; and it cannot tell a service that came back thirty seconds ago
    from one on a fresh installation that nobody has searched yet. That ambiguity is the
    whole reason it is a warning while an unreachable reranker is a failure — and the
    reason `demo-check.sh`, which can read the exact answer, also asks.
    """
    url = settings.tei_rerank_url.rstrip("/")
    shown = redact(url)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            health = await client.get(f"{url}/health")
            if health.status_code != 200:
                return (
                    "fail",
                    f"{shown} answered /health with {health.status_code}. {_RERANKER_COST}",
                )
            # Best effort, like `/info` above: a TEI without `/metrics` is still a working
            # reranker, and failing the check over a missing counter would cry wolf.
            try:
                metrics = await client.get(f"{url}/metrics")
                # The status code decides, never the body: TEI answers `200` with an empty
                # body when it has served nothing, and that emptiness is the signal.
                served = _requests_served(metrics.text if metrics.status_code == 200 else None)
            except httpx.HTTPError:
                served = None
    except httpx.HTTPError as exc:
        # The exception name, because `degradation.py` sends it here on purpose: the reader
        # gets a sentence about their results, and the person who can fix it gets the cause.
        return "fail", f"{shown} is not answering ({type(exc).__name__}). {_RERANKER_COST}"

    note = _breaker_note()
    if served is None:
        return "ok", f"{shown} healthy; no /metrics, so a restart is invisible from here.{note}"
    if served == 0:
        return "warn", (
            f"{shown} is up and has answered 0 requests since it last started, which is what "
            f"a restart looks like from in here. `docker inspect` has the real count.{note}"
        )
    return "ok", f"{shown} healthy, {served} request(s) answered since it last started.{note}"


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
        # After migrations, because "declared but absent" only means anything once the
        # database is known to be at head — before that it is the migration state saying the
        # same thing twice.
        await _timed("bypass surface", _security_definer_surface),
        # Beside the bypass surface because it is the same kind of question and the same kind
        # of answer: a property of the *running* installation that no test built from the
        # migrations can see. What a schema declares about partitioning and what a server was
        # started with are independent, and only one of them causes a 500.
        await _timed("lock budget", _lock_budget),
        # Right after migrations, because it is the half of the install that `alembic upgrade
        # head` does not do and that nothing else would report as missing.
        await _timed("job queue", _job_queue),
        await _timed("extensions", _extensions),
        await _timed("content", _content),
        await _timed("document storage", _storage),
        # After storage, because it needs the storage root to be readable to mean anything.
        await _timed("document files", _orphaned_documents),
        await _timed("stranded documents", _stranded_documents),
        await _timed("hardware profile", _hardware),
        await _timed("vector space", _vector_space),
        await _timed("embedding service", _model_service("embed", settings.tei_embed_url)),
        await _timed("reranking service", _model_service("rerank", settings.tei_rerank_url)),
        # Last, and separate from the line above it. `reranking service` asks the question
        # every model endpoint is asked — are you there, what are you serving. This asks the
        # one that went unanswered on 28 August: is the component the recall number depends
        # on actually present, and has it just come back from the dead.
        await _timed("reranker health", _reranker_health),
    ]
