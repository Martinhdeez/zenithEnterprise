"""The install CLI (mvp.md §2.4).

A freshly installed system has no user, and there is no open signup, so something has to
create the first one. That something must not be a hand-written `INSERT` in `psql`: it
is error-prone, it leaves a password hash in the shell history, and it is not something
you can hand to a customer as an installation step.

Everything here runs on the owner connection, which **bypasses RLS**. That is
unavoidable — a tenant has to exist before anything can be scoped to it — and it is why
this file reuses the service layer instead of writing its own SQL. The rules that keep
it safe:

- Passwords are **generated here and printed once**, never taken as an argument. An
  argument ends up in the shell history and in the process table, visible to every other
  user on the machine.
- This is the only path allowed to bypass invitations. Everything else goes through the
  API, under a permission check.
"""

import asyncio
import json
import secrets
import string
from collections.abc import Coroutine
from typing import Annotated, Any

import typer
from sqlalchemy import select

from app.common.exceptions import ConflictError, NotFoundError
from app.core.config import generate_secret
from app.core.database import dispose_engines, owner_session
from app.core.diagnostics import run_diagnostics
from app.features.auth.model import Role, User
from app.features.auth.provisioning import create_user
from app.features.auth.service import normalise_email
from app.features.tenancy.model import Tenant
from app.features.tenancy.service import TenantService

app = typer.Typer(help="Zenith Enterprise installation and recovery commands.")

# Unambiguous alphabet: no O/0, no l/1/I. These passwords get read aloud over the phone
# and typed from a screenshot, and a character nobody can identify is a support call.
_ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "O0oIl1")


def _generate_password(length: int = 20) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def _execute(work: Coroutine[Any, Any, None]) -> None:
    """Run one command's work in its own event loop, closing connections afterwards.

    The disposal is not tidiness. An async engine belongs to the loop that created it,
    so leaving pooled connections behind means the next command in the same process
    inherits handles bound to a loop that no longer exists — which is exactly the shape
    of a test suite invoking several commands in a row.
    """

    async def runner() -> None:
        try:
            await work
        finally:
            await dispose_engines()

    asyncio.run(runner())


def _print_credentials(email: str, password: str) -> None:
    typer.echo("")
    typer.secho("  Save this password now — it is not stored and cannot be", fg=typer.colors.YELLOW)
    typer.secho("  shown again. Only its hash is kept.", fg=typer.colors.YELLOW)
    typer.echo("")
    typer.echo(f"  email:    {email}")
    typer.secho(f"  password: {password}", bold=True)
    typer.echo("")


@app.command()
def create_tenant(
    name: Annotated[str, typer.Argument(help="Company name.")],
    admin_email: Annotated[str, typer.Option(help="Address of the first administrator.")],
) -> None:
    """Create a tenant and its first administrator.

    The tenant is created with its two system roles, and the administrator is given
    `admin`, which holds the whole permission catalogue. Without that, the first
    administrator is born unable to administer anything.
    """

    async def run() -> None:
        try:
            tenant = await TenantService().create(name)
        except ConflictError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from exc

        password = _generate_password()
        async with owner_session() as session:
            admin_role = await session.scalar(
                select(Role).where(Role.tenant_id == tenant.id, Role.name == "admin")
            )
            assert admin_role is not None, "create() seeds the system roles"
            await create_user(session, tenant.id, admin_email, password, [admin_role.id])

        typer.secho(f"Tenant {tenant.name!r} created.", fg=typer.colors.GREEN)
        _print_credentials(normalise_email(admin_email), password)

    _execute(run())


@app.command()
def invite(
    tenant: Annotated[str, typer.Option(help="Tenant name.")],
    email: Annotated[str, typer.Option(help="Address of the new user.")],
    role: Annotated[str, typer.Option(help="Role name, as shown by list-tenants.")],
) -> None:
    """Create a user in an existing tenant and give them a role."""

    async def run() -> None:
        try:
            found = await TenantService().by_name(tenant)
        except NotFoundError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from exc

        password = _generate_password()
        async with owner_session() as session:
            target = await session.scalar(
                select(Role).where(Role.tenant_id == found.id, Role.name == role)
            )
            if target is None:
                typer.secho(f"no role named {role!r} in {tenant!r}", fg=typer.colors.RED, err=True)
                raise typer.Exit(1)
            await create_user(session, found.id, email, password, [target.id])

        typer.secho(f"User created in {found.name!r} with role {role!r}.", fg=typer.colors.GREEN)
        _print_credentials(normalise_email(email), password)

    _execute(run())


@app.command()
def reset_password(
    tenant: Annotated[str, typer.Option(help="Tenant name.")],
    email: Annotated[str, typer.Option(help="Address of the user.")],
) -> None:
    """Set a new password for a user and invalidate their existing sessions.

    This exists from the first release, not as a convenience. There is no outbound mail
    configured on-premise, so without it the first administrator who forgets their
    password leaves the whole installation inaccessible.
    """

    async def run() -> None:
        from app.core.security import hash_password

        password = _generate_password()
        async with owner_session() as session:
            user = await session.scalar(
                select(User)
                .join(Tenant, Tenant.id == User.tenant_id)
                .where(Tenant.name == tenant, User.email == normalise_email(email))
            )
            if user is None:
                typer.secho(f"no user {email!r} in {tenant!r}", fg=typer.colors.RED, err=True)
                raise typer.Exit(1)
            user.password_hash = hash_password(password)
            # Whoever needed a reset may have had their account compromised. Leaving the
            # old sessions alive would make the reset cosmetic.
            user.token_version += 1

        typer.secho("Password reset. Existing sessions were invalidated.", fg=typer.colors.GREEN)
        _print_credentials(normalise_email(email), password)

    _execute(run())


@app.command()
def list_tenants() -> None:
    """List the tenants of this installation, with their roles and user counts."""

    async def run() -> None:
        tenants = await TenantService().list_all()
        if not tenants:
            typer.echo("No tenants yet. Create one with `zenith create-tenant`.")
            return

        async with owner_session() as session:
            for tenant in tenants:
                roles = list(
                    await session.scalars(
                        select(Role.name).where(Role.tenant_id == tenant.id).order_by(Role.name)
                    )
                )
                users = len(
                    list(await session.scalars(select(User.id).where(User.tenant_id == tenant.id)))
                )
                typer.echo(f"{tenant.name}  ({users} users)")
                typer.echo(f"  roles: {', '.join(roles) or 'none'}")

    _execute(run())


@app.command()
def diagnose(
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable output, for attaching to a ticket.")
    ] = False,
) -> None:
    """Report the health of this installation.

    Built to be run by an operator we cannot reach and pasted into an email. It never
    prints a secret — connection strings are redacted — and it never aborts on a failed
    check, because the checks most worth running are the ones that fail.

    Exit code is 1 if anything failed, so it also works as a smoke test after an install.
    """

    async def run() -> None:
        checks = await run_diagnostics()

        if as_json:
            typer.echo(json.dumps({"checks": [check.as_dict() for check in checks]}, indent=2))
        else:
            colours = {
                "ok": typer.colors.GREEN,
                "warn": typer.colors.YELLOW,
                "fail": typer.colors.RED,
            }
            for check in checks:
                typer.secho(f"  {check.status.upper():<5}", fg=colours[check.status], nl=False)
                typer.echo(f"{check.name:<28} {check.detail}  ({check.elapsed_ms:.0f}ms)")

        if any(check.status == "fail" for check in checks):
            raise typer.Exit(1)

    _execute(run())


@app.command()
def reingest(
    tenant: Annotated[str | None, typer.Option(help="Limit to one tenant, by name.")] = None,
    status: Annotated[str | None, typer.Option(help="Only this status: pending or failed.")] = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Actually enqueue. Without it, only report.")
    ] = False,
) -> None:
    """Put stranded documents back in the ingestion queue.

    Two paths leave a document with no job behind it, both deliberate: a failed enqueue does
    not fail the upload, and a document relabelled after enqueue strands its own job because
    the payload carries the labels captured at upload. This is the fix for both.

    Reports by default. Enqueuing a thousand documents on a machine sized for one at a time
    is an operator's decision, not a side effect of asking what is stuck.
    """
    from app.features.ingestion.requeue import find_stranded, requeue

    async def run() -> None:
        tenant_id = (await TenantService().by_name(tenant)).id if tenant else None
        stranded = await find_stranded(tenant_id, status)
        if not stranded:
            typer.echo("Nothing stranded.")
            return

        for document in stranded:
            labels = "no labels — will be skipped" if not document.label_ids else ""
            typer.echo(f"{document.status:<8} {document.filename}  {labels}")

        if not apply:
            typer.echo(f"\n{len(stranded)} document(s). Re-run with --apply to enqueue.")
            return

        typer.echo(f"\nEnqueued {await requeue(stranded)} document(s).")

    _execute(run())


@app.command("install-queue")
def install_queue() -> None:
    """Create the job-queue tables. Run once, after `alembic upgrade head`.

    Procrastinate owns its own schema and manages it itself, so it is not part of our
    migrations: mixing the two would mean our `downgrade` had opinions about a library's
    tables. Separate command, run at install time, and the worker refuses to start without
    it — which is the loud failure we want rather than jobs vanishing into a missing table.
    """
    import asyncio

    from app.features.ingestion.tasks import build_app

    async def apply() -> None:
        queue = build_app()
        async with queue.open_async():
            await queue.schema_manager.apply_schema_async()

    asyncio.run(apply())
    typer.echo("Job-queue schema installed.")


@app.command("generate-secret")
def generate_jwt_secret() -> None:
    """Print a secret suitable for ZENITH_JWT_SECRET.

    The startup validator rejects placeholders and anything under 32 bytes, but it
    cannot tell a considered choice from thirty-two identical characters. This removes
    the choice, which is the only reliable fix.
    """
    typer.echo(generate_secret())


if __name__ == "__main__":
    app()
