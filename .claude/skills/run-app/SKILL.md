---
name: run-app
description: Start Zenith Enterprise locally via Docker Compose — backend API, worker, Postgres (ParadeDB), the embeddings model, and the frontend. No reranker, no local LLM (chat/generation goes through the Gemini API, configured in-app under Admin, not via env var). Use whenever asked to run/start/launch the app, or to verify a change in the real running app.
---

# Running Zenith Enterprise

Everything runs via `docker/docker-compose.yml`. There is no root `.env` checked into
git — the repo's `backend/.env` is for the *non-docker* `uv run` workflow and uses
`localhost` hostnames that don't resolve inside containers.

## One-time setup (fresh Postgres volume only)

Skip this if `docker volume ls | grep zenith_db_data` already exists and a previous run
already did it — check `docker compose logs api` for `RuntimeError: RLS is not active`
to know if it's needed again (e.g. after `docker compose down -v`).

1. Create root `.env` (`docker/docker-compose.yml`'s `env_file: [../.env]`) — copy the
   values `ZENITH_JWT_SECRET`, `ZENITH_ENCRYPTION_KEY` from `backend/.env`, plus:
   ```
   POSTGRES_USER=zenith
   POSTGRES_PASSWORD=zenith
   POSTGRES_DB=zenith
   ZENITH_APP_PASSWORD=zenith_app
   ZENITH_STORAGE_DIR=/data/documents   # path inside the container, not the host
   ZENITH_HARDWARE=low-spec
   ```
2. Nothing runs migrations automatically — `app/main.py`'s lifespan does not call
   alembic, and no compose service has an entrypoint that does either. After `db` is
   healthy, run them by hand once:
   ```bash
   docker compose exec api alembic upgrade head
   ```
   Then the other half of the install, which the migrations deliberately do not cover —
   Procrastinate manages its own schema:
   ```bash
   docker compose exec api zenith install-queue
   ```
   Skip it and the app accepts uploads and ingests none: 201, a row, and a status that stays
   `pending` for ever. `zenith diagnose` reports the missing tables.
3. Migration `0001_initial_schema.py` creates the `zenith_app` role `NOLOGIN` — nothing
   in the repo ever gives it a password, so right after that first `alembic upgrade head`:
   ```bash
   docker compose exec db psql -U zenith -d zenith -c \
     "ALTER ROLE zenith_app LOGIN PASSWORD 'zenith_app';"
   ```
   Must match `ZENITH_APP_PASSWORD` in `.env`. Without this, `api`/`worker` fall back to
   connecting as the schema owner and `verify_rls_active()` refuses to start — by design,
   silently disabling RLS is the one failure mode that check exists to prevent.
4. Migration `0010` creates a second role the same way, for the same reason. `zenith_platform`
   bypasses RLS and holds no DDL; it is what the system administration panel connects as
   (`app/core/database.py`'s `platform_session`):
   ```bash
   docker compose exec db psql -U zenith -d zenith -c \
     "ALTER ROLE zenith_platform LOGIN PASSWORD 'zenith_platform';"
   ```
   Must match `ZENITH_PLATFORM_PASSWORD` in `.env` (default `zenith_platform`). Only the
   `/system/*` routes use it, so the rest of the app runs fine without it — the panel is
   where the omission shows up.
5. Nobody can reach `/system` until somebody is granted it, and it cannot be granted from
   inside the product (that is the point — see migration 0010). Bootstrap the first one:
   ```bash
   docker compose exec api uv run python -m app.cli grant-system-admin you@example.com
   ```

## Start it

```bash
open -a Docker  # if the daemon isn't already up
cd docker
docker compose up -d --build db tei-embed tei-rerank api worker frontend
```

`tei-rerank` is in that list on purpose, and it did not use to be. It is optional in the
sense that `SearchService` catches its absence and answers anyway from the fused order,
marked `degraded` — nothing crashes and no error is logged. It is not optional in the sense
that matters: `docker/docker-compose.yml`'s own comment records that a reranker which never
runs costs **about 15 points of recall**, and the demonstration claims Recall@8 of 90.0%
(`.artifacts/specs/2026-08-26-what-this-demo-claims.md`). Starting the stack without it is
how you demonstrate a number you cannot reproduce, with no symptom in front of you.
`./scripts/demo-check.sh` fails on it for that reason.

Genuinely excluded: any local LLM service — none is defined in this compose file at all,
because chat generation goes through whatever provider is configured in Admin → LLM
connector, e.g. Gemini.

## Verify it's actually up

```bash
curl -s http://localhost:8000/openapi.json -o /dev/null -w "api: %{http_code}\n"
curl -s http://localhost:5173/ -o /dev/null -w "frontend: %{http_code}\n"
curl -s http://localhost:8081/health -o /dev/null -w "tei-embed: %{http_code}\n"
docker compose logs api --tail=20   # look for "Application startup complete", not a
                                     # RuntimeError traceback
```

Open http://localhost:5173 in a browser — that's the real app, nginx-proxying
`/auth|labels|documents|search|query|tenant|roles|llm-config|users` through to `api:8000`
(see `docker/nginx.frontend.conf`; keep that prefix list in sync with `app/main.py`'s
`include_router` calls the same way `frontend/vite.config.ts`'s dev proxy is).

That same config also gives `.mjs` files `text/javascript` explicitly — nginx's stock
`mime.types` has no entry for it, so without this the PDF viewer's pdf.js worker script
loads as `application/octet-stream` and the browser's strict module-script MIME check
silently refuses to run it. Every citation click and document preview then fails with
"The document could not be rendered.", which looks like a storage or backend problem and
is neither. If this regresses after editing the nginx conf, a normal browser reload may
not be enough to see the fix — Chrome can hold the old `.mjs` response (wrong
content-type and all) in its disk cache under the same hashed filename; a hard reload
(Cmd/Ctrl+Shift+R) forces it to re-fetch.

## Document storage

`api` and `worker` both bind-mount `../backend/.data/documents` to `/data/documents`
(matching `ZENITH_STORAGE_DIR` in `.env`) rather than using a named volume. Documents are
content-addressed by sha256 under that root (`documents/storage.py`), and this repo's
`db_data` volume already has rows pointing at files that were written there by earlier
non-docker (`uv run`) sessions. A named volume would start empty with those rows now
pointing at nothing, which the PDF viewer reports as "That document is no longer
available." — not a rendering problem, a storage problem, and a different one from the
`.mjs` issue above even though both surface in the same panel.

## After changing backend code

Images are a build-time snapshot, not bind-mounted — editing `.py` files does nothing
until the image is rebuilt:
```bash
docker compose up -d --build api worker
```
**Rebuild `worker` too, not just `api`.** They are separate images from the same source, and
a new Procrastinate task registered in `ingestion/tasks.py` exists only in the image that was
rebuilt. A stale worker accepts the job and fails it with `Task was not found` — which reads
like a queue problem and is a build problem.
And if the change added an Alembic migration, apply it the same way as initial setup:
```bash
docker compose exec api alembic upgrade head
```

**`make check` passing does not mean the dev stack is migrated, and this has already
bitten once.** `pytest` builds a throwaway Postgres per run and migrates it from zero, so
a brand-new migration is exercised there whatever state the long-lived `db_data` volume is
in. Migration 0007 added `access_labels.created_at`, the suite went green, and every
`POST /labels` in the running app answered `500 UndefinedColumn` until someone tried to
create a label by hand. After adding a migration, check the dev database itself:
```bash
docker compose exec api alembic current   # must equal `head`, not just "no errors"
```

## Ports

| Service | Port |
|---|---|
| frontend | 5173 |
| api | 8000 |
| db (Postgres) | 5432 |
| tei-embed | 8081 |
| tei-rerank | 8082 |

## Stopping

```bash
docker compose down          # keeps db_data/hf_cache volumes — next `up` skips the one-time setup
docker compose down -v       # wipes them — next `up` needs the one-time setup again
```
