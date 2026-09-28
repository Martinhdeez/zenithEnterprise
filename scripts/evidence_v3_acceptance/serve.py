"""Disposable real application for browser acceptance; never targets an existing DB.

Run from backend with `uv run python ../scripts/evidence_v3_acceptance/serve.py`.
Requires the pinned local TEI services on 18081/18083 and installed frontend deps.
Credentials and logs go only to the ignored .scratch directory. Ctrl-C cleans up.
"""

import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx
from testcontainers.community.postgres import PostgresContainer

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / ".scratch/release-validation"
OUT.mkdir(parents=True, exist_ok=True)
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def provision(owner_url):
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.database import owner_session
    from app.features.auth.model import Role
    from app.features.auth.onboarding.provisioning import create_user
    from app.features.labels.model import AccessLabel, RoleLabel
    from app.features.tenancy.service import TenantService
    from app.models import Base  # noqa: F401

    engine = create_async_engine(owner_url)
    async with engine.begin() as conn:
        await conn.execute(text("ALTER ROLE zenith_app LOGIN PASSWORD 'acceptance-app'"))
        await conn.execute(text("ALTER ROLE zenith_platform LOGIN PASSWORD 'acceptance-platform'"))
    await engine.dispose()
    fixture = {"users": {}, "labels": {}, "documents": {}}
    for key in ("alpha", "beta"):
        tenant = await TenantService().create(f"Acceptance {key}")
        async with owner_session() as session:
            roles = {
                r.name: r
                for r in await session.scalars(select(Role).where(Role.tenant_id == tenant.id))
            }
            default = AccessLabel(tenant_id=tenant.id, name=f"{key} public")
            harbor = AccessLabel(tenant_id=tenant.id, name=f"{key} harbor")
            private = AccessLabel(tenant_id=tenant.id, name=f"{key} private")
            session.add_all([default, harbor, private])
            await session.flush()
            session.add(RoleLabel(role_id=roles["admin"].id, label_id=private.id))
            for role in roles.values():
                session.add(RoleLabel(role_id=role.id, label_id=default.id))
                session.add(RoleLabel(role_id=role.id, label_id=harbor.id))
            fixture["labels"][key] = {
                "default": str(default.id),
                "private": str(private.id),
                "harbor": str(harbor.id),
            }
            for role in ("admin", "member"):
                email = f"{key}-{role}@example.com"
                password = secrets.token_urlsafe(24)
                await create_user(session, tenant.id, email, password, [roles[role].id])
                fixture["users"][f"{key}-{role}"] = {"email": email, "password": password}
    return fixture


def wait_http(url, processes):
    end = time.monotonic() + 180
    while time.monotonic() < end:
        assert all(p.poll() is None for p in processes), "Service exited; inspect private logs"
        try:
            if httpx.get(url, timeout=3).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise TimeoutError(url)


def main():
    processes, logs = [], []
    # Scrub inherited experimental settings and credentials. No Jev call is possible.
    env = {k: v for k, v in os.environ.items() if not k.startswith("ZENITH_")}
    # Explicit overrides also isolate Settings' backend/.env file, which may
    # contain the separately authorized public-study credential.
    env.update(
        ZENITH_JEV_API_KEY="",
        ZENITH_EVIDENCE_JUDGE_PROVIDER="tei",
        ZENITH_EXTERNAL_PROCESSING_FOR_RERANKING="false",
        ZENITH_EXTERNAL_PROCESSING_FOR_SEGMENTATION="false",
        ZENITH_EXTERNAL_PROCESSING_FOR_CLAIM_SUPPORT="false",
        ZENITH_DIRECT_ENABLED="false",
        ZENITH_EVIDENCE_PACKETS_ENABLED="false",
        ZENITH_EVIDENCE_COUNTEREVIDENCE_ENABLED="false",
        ZENITH_STRICT_CLAIM_SUPPORT_ENABLED="false",
        ZENITH_LLM_PROVIDER="openai",
    )
    env.update(
        PYTHONUTF8="1",
        PYTHONPATH=os.pathsep.join(
            [
                str(ROOT / "scripts/windows_selector_bootstrap"),
                str(BACKEND),
                str(Path(__file__).parent),
            ]
        ),
    )
    with PostgresContainer("paradedb/paradedb:0.15.26-pg17", driver="psycopg").with_command(
        "postgres -c max_locks_per_transaction=2560"
    ) as db:
        host, port = db.get_container_host_ip(), db.get_exposed_port(5432)
        owner = f"postgresql+psycopg://{db.username}:{db.password}@{host}:{port}/{db.dbname}"
        env.update(
            ZENITH_DATABASE_OWNER_URL=owner,
            ZENITH_DATABASE_URL=f"postgresql+psycopg://zenith_app:acceptance-app@{host}:{port}/{db.dbname}",
            ZENITH_DATABASE_PLATFORM_URL=f"postgresql+psycopg://zenith_platform:acceptance-platform@{host}:{port}/{db.dbname}",
            ZENITH_JWT_SECRET=secrets.token_urlsafe(48),
            ZENITH_STORAGE_DIR=str(OUT / "documents"),
            ZENITH_TEI_EMBED_URL="http://127.0.0.1:18081",
            ZENITH_TEI_RERANK_URL="http://127.0.0.1:18083",
            ZENITH_LLM_ENDPOINT_URL="http://127.0.0.1:18999/v1",
        )
        os.environ.update(env)
        with (OUT / "install.log").open("w", encoding="utf-8") as log:
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=BACKEND,
                env=env,
                stdout=log,
                stderr=log,
                check=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "procrastinate",
                    "--app=app.features.ingestion.tasks.app",
                    "schema",
                    "--apply",
                ],
                cwd=BACKEND,
                env=env,
                stdout=log,
                stderr=log,
                check=True,
            )
        fixture = asyncio.run(provision(owner))
        fixture["head"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        fixture["base_url"] = "http://127.0.0.1:18110"
        fixture["experimental_url"] = "http://127.0.0.1:18111"

        def start(name, args, cwd, extra=None):
            log = (OUT / f"{name}.log").open("w", encoding="utf-8")
            logs.append(log)
            p = subprocess.Popen(
                args,
                cwd=cwd,
                env={**env, **(extra or {})},
                stdout=log,
                stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            processes.append(p)
            return p

        try:
            start(
                "api", [sys.executable, str(Path(__file__).with_name("api.py")), "18100"], BACKEND
            )
            start(
                "worker",
                [
                    sys.executable,
                    "-m",
                    "procrastinate",
                    "--app=app.features.ingestion.tasks.app",
                    "worker",
                    "--concurrency",
                    "1",
                ],
                BACKEND,
            )
            start(
                "vite",
                [
                    "node",
                    "node_modules/vite/bin/vite.js",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "18110",
                    "--strictPort",
                ],
                ROOT / "frontend",
                {
                    "VITE_API_PROXY_TARGET": "http://127.0.0.1:18100",
                    "ZENITH_VITE_CACHE_DIR": str(
                        OUT / f"vite-cache-default-{secrets.token_hex(8)}"
                    ),
                },
            )
            for index, name, overrides in (
                (1, "experimental", {}),
                (2, "strict", {"ZENITH_STRICT_CLAIM_SUPPORT_ENABLED": "true"}),
                (
                    3,
                    "unavailable",
                    {
                        "ZENITH_TEI_RERANK_URL": "http://127.0.0.1:18998",
                        "ZENITH_DIRECT_MAX_UNITS": "16",
                    },
                ),
            ):
                extra = {
                    "ZENITH_DIRECT_ENABLED": "true",
                    "ZENITH_DIRECT_MAX_UNITS": "4",
                    "ZENITH_EVIDENCE_PACKETS_ENABLED": "true",
                    "ZENITH_LLM_PROVIDER": "mock",
                    **overrides,
                }
                start(
                    name,
                    [sys.executable, str(Path(__file__).with_name("api.py")), str(18100 + index)],
                    BACKEND,
                    extra,
                )
                start(
                    f"vite-{name}",
                    [
                        "node",
                        "node_modules/vite/bin/vite.js",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(18110 + index),
                        "--strictPort",
                    ],
                    ROOT / "frontend",
                    {
                        "VITE_API_PROXY_TARGET": f"http://127.0.0.1:{18100 + index}",
                        "ZENITH_VITE_CACHE_DIR": str(
                            OUT / f"vite-cache-{name}-{secrets.token_hex(8)}"
                        ),
                    },
                )
                fixture[f"{name}_url"] = f"http://127.0.0.1:{18110 + index}"
            wait_http(fixture["base_url"] + "/auth/me", processes)
            from reportlab.pdfgen.canvas import Canvas

            pdf = OUT / "acceptance-rates.pdf"
            canvas = Canvas(str(pdf))
            for line in (
                "Harbor manual. General introduction.",
                "Saffron submarines pay exactly seven credits.",
                "Appendix. Amber vessels pay nine credits.",
            ):
                canvas.drawString(72, 740, line)
                canvas.drawString(
                    72,
                    715,
                    "This synthetic harbor manual is provided for local acceptance testing.",
                )
                canvas.drawString(
                    72,
                    690,
                    "Each vessel must retain its registration and follow the harbor schedule.",
                )
                canvas.showPage()
            canvas.save()
            sources = {
                "text": (
                    "acceptance-observatory.txt",
                    b"Observatory handbook.\nThe cobalt telescope opens at midnight.\n"
                    b"Visitors must book ahead and bring their registration receipt "
                    b"to the entrance.\n",
                    "alpha",
                    "default",
                ),
                "pdf": (pdf.name, pdf.read_bytes(), "alpha", "harbor"),
                "label-hidden": (
                    "private-lantern.txt",
                    b"Hiddenlabelcanary lantern reserve is 813 credits. "
                    b"This synthetic confidential ledger belongs only to the "
                    b"authorized finance compartment.",
                    "alpha",
                    "private",
                ),
                "tenant-hidden": (
                    "private-orchid.txt",
                    b"Hiddentenantcanary orchid reserve is 927 credits. "
                    b"This synthetic confidential ledger belongs only to the "
                    b"other tenant organization.",
                    "beta",
                    "default",
                ),
            }
            with httpx.Client(base_url=fixture["base_url"], timeout=30) as client:
                for key, (name, body, tenant, label) in sources.items():
                    login = client.post("/auth/login", json=fixture["users"][f"{tenant}-admin"])
                    login.raise_for_status()
                    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
                    uploaded = client.post(
                        "/documents",
                        headers=headers,
                        files={"file": (name, body)},
                        data={"labels": fixture["labels"][tenant][label]},
                    )
                    uploaded.raise_for_status()
                    doc = uploaded.json()["document"]
                    assert doc["status"] == "pending", doc
                    fixture["documents"][key] = {
                        "id": doc["id"],
                        "filename": name,
                        "sha256": __import__("hashlib").sha256(body).hexdigest(),
                    }
                    deadline = time.monotonic() + 180
                    while time.monotonic() < deadline:
                        status = client.get(f"/documents/{doc['id']}", headers=headers)
                        status.raise_for_status()
                        if status.json()["status"] == "ready":
                            break
                        assert status.json()["status"] != "failed", status.json()
                        time.sleep(1)
                    else:
                        raise TimeoutError(f"Ingestion not ready: {key}")
            (OUT / "fixture.json").write_text(json.dumps(fixture, indent=2), encoding="utf-8")
            print(
                "READY: real API, worker, Vite, disposable DB; four documents ready",
                flush=True,
            )
            while not (OUT / "stop").exists():
                assert all(p.poll() is None for p in processes), "Service exited"
                time.sleep(2)
        finally:
            for p in reversed(processes):
                p.terminate()
            for p in processes:
                p.wait(timeout=15)
            for log in logs:
                log.close()


if __name__ == "__main__":
    main()
