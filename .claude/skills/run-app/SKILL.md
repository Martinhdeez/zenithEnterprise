---
name: run-app
description: Start Zenith Enterprise locally via Docker Compose — backend API, worker, Postgres (ParadeDB), the embeddings and reranking models, and the frontend. No local LLM (chat/generation goes through the Gemini API, configured in-app under Admin, not via env var). Use whenever asked to run/start/launch the app, or to verify a change in the real running app.
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

## Which directory compose runs from decides everything

**A relative path in a compose file is resolved against the *project directory*, not against
the file.** `docker/docker-compose.yml` is built almost entirely out of relative paths —
`build.context: ..` on `api`, `worker` and `frontend`, `env_file: [../.env]`, and
`../backend/.data/documents:/data/documents` on `api` and `worker` — so that one fact decides
the code you build, the documents you mount and the settings you get. The project directory
defaults to the directory holding the first `-f` file; `--project-directory` overrides it for
all of them at once.

Three failures in one day came out of that sentence, and none of them named it:

- Compose run from a worktree mounts *that worktree's* `backend/.data/documents`, which is
  empty. Everything starts, every row is still there, and **every PDF 404s**. Not a storage
  bug. This has happened twice.
- Compose run from a checkout on an older branch uses *that* file, so a recreate left
  `max_locks_per_transaction` at Postgres's default (below).
- `--project-directory` pointing at one checkout while `-f` points at another builds **the
  first one's source**, whatever `-f` says, because `build.context` is a relative path like
  every other. That produced images without migration 0026 against a database that had it,
  and `alembic upgrade head` answered `Can't locate revision identified by '0026'`.

`docker compose config` prints every path fully resolved and starts nothing. When in doubt,
run it and read `context:` and `source:`.

## Start it

If the checkout you want to run is also the one holding `backend/.data/documents` and `.env`
— the usual case — there is nothing to decide:

```bash
open -a Docker  # if the daemon isn't already up
cd docker
docker compose up -d --build db tei-embed tei-rerank api worker frontend
```

When they are **different** checkouts — a worktree's code against the main checkout's
documents — building and running need two invocations, because no single project directory is
right for both:

```bash
CODE=/path/to/the/checkout/whose/code/you/want
DATA=/path/to/zenithEnterprise   # the one holding backend/.data/documents and .env

# Build with no --project-directory, so `build.context: ..` resolves against $CODE.
(cd "$CODE/docker" && docker compose -p zenith build api worker frontend)

# Run with the file from $CODE and every relative path from $DATA. --no-build, because the
# images exist and building here would build the other checkout's source.
RUN="docker compose -p zenith -f $CODE/docker/docker-compose.yml --project-directory $DATA/docker"
$RUN up -d --no-build --force-recreate db
$RUN up -d --no-build tei-embed tei-rerank api worker frontend
```

`-p zenith` on **both**. The image names compose derives — `zenith-api`, `zenith-worker`,
`zenith-frontend` — come from the project name, and it is what makes the build and the run
mean the same images.

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

## `max_locks_per_transaction`, and why `db` is recreated rather than restarted

`chunks` and `chunk_embeddings` have been partitioned since migration 0026 — `HASH
(tenant_id)`, `ZENITH_PARTITION_MODULUS` buckets each, 128 by default. The planner takes an
`AccessShareLock` on every relation of every partition **before** runtime pruning removes
anything, so one dense search takes `9 x 128 + 9` = **1,161 locks** (nine measured per
partition-pair, `eval/lock-budget.json`).

`max_locks_per_transaction` does not cap a transaction. It sizes one table of
`max_locks_per_transaction x (max_connections + max_prepared_transactions)` slots that the
whole cluster draws from, so what it bounds is *concurrency*. At Postgres's default of 64
that is 6,400 slots — five concurrent searches — and past it
`psycopg.errors.OutOfMemory: out of shared memory` is raised **during planning**. The query
never runs, so an ordinary search is an HTTP 500 rather than a slow answer. (The measured
bracket sits higher than the nominal one, because the lock hash table grows into shared
memory nobody reserved; that surplus is transient and shared with every other backend, so the
nominal number is the one to size against.) `docker/docker-compose.yml` sets it to 2,560.

**It is a start-up flag on the container's `command:`, so it takes effect only when `db` is
RECREATED.** `docker compose restart db` restarts the container that already exists, with the
value it already had, and reports nothing wrong. That happened today.

```bash
docker compose up -d --force-recreate --no-build db
```

`zenith diagnose` reports the value actually in force, which is the only way to tell a correct
value in the repository from a correct value in the running database.

## Verify it's actually up

```bash
curl -s http://localhost:8000/openapi.json -o /dev/null -w "api: %{http_code}\n"
curl -s http://localhost:5173/ -o /dev/null -w "frontend: %{http_code}\n"
curl -s http://localhost:8081/health -o /dev/null -w "tei-embed: %{http_code}\n"
docker compose logs api --tail=20   # look for "Application startup complete", not a
                                     # RuntimeError traceback
docker compose exec api zenith diagnose
```

**`make check` says the code is correct; `zenith diagnose` says the *installation* is
correct.** They are different questions and the second is the one that has failed in front of
people. It is the fastest way to see whether a running stack is sane: it reads the migration
state, the lock budget above, whether the installed modulus fits *this* corpus, the
`SECURITY DEFINER` bypass surface and the reranker off the running database rather than off
the repository, and exits 1 if anything failed.

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
And if the change added an Alembic migration, **the order matters, and 0026 is why.** It makes
`query_citations.tenant_id` `NOT NULL`, so a pre-0026 application image writing a citation
against a post-0026 database fails on the first one: migrating before rebuilding gets you a
stack that starts and then breaks on the first write. Settings, then code, then schema, then
statistics:

```bash
docker compose up -d --force-recreate --no-build db   # only if the compose file changed
docker compose up -d --build api worker frontend
docker compose exec api alembic upgrade head
docker compose exec db psql -U zenith -d zenith -c "ANALYZE chunks; ANALYZE chunk_embeddings;"
```

**The `ANALYZE` is part of the migration, not hygiene after it.** 0026 does not analyse the
partitions it creates, so until autovacuum has reached all 128 of each the planner's row
estimates cover a fraction of the corpus. `zenith diagnose` warns until it is run, and says
why: a modulus sized on part of the corpus is worse than none.

**Migration 0027 adds the schema for a projected 512-dimensional space and does not install
one.** It creates `embedding_space_axes` and the `source_dimension`/`basis_digest` columns and
inserts no space, because fitting a basis needs numpy and numpy is deliberately kept out of
the shipped image — `eval/svd_512.py` fits one from a checkout where it exists, and there is
no `zenith fit-basis` command. An installation at head with no projected space searches the
1024-dimensional space and is entirely correct. There is no missing step to hunt for.

**`make check` passing does not mean the dev stack is migrated, and this has already
bitten once.** `pytest` builds a throwaway Postgres per run and migrates it from zero, so
a brand-new migration is exercised there whatever state the long-lived `db_data` volume is
in. Migration 0007 added `access_labels.created_at`, the suite went green, and every
`POST /labels` in the running app answered `500 UndefinedColumn` until someone tried to
create a label by hand. After adding a migration, check the dev database itself:
```bash
docker compose exec api alembic current   # must equal `head`, not just "no errors"
```

## Symptoms that point somewhere other than where they seem

| What you see | What it actually is |
|---|---|
| **Every PDF 404s** | The container's document store is empty — compose ran with the wrong project directory, not a storage bug |
| **A search 500s under light concurrent load** | The lock table, not the query — `max_locks_per_transaction`, and `db` needs *recreating* |
| `Can't locate revision identified by '0026'` | The image was built from a checkout that does not have the migration the database has |
| "That document is no longer available." | Rows pointing at files the container cannot see — the mount root, not the renderer |
| "The document could not be rendered." | The `.mjs` MIME type in `nginx.frontend.conf`; hard-reload afterwards |
| Uploads answer 201 and stay `pending` for ever | `zenith install-queue` was never run |
| `Task was not found` | A stale `worker` image — rebuild it too, not only `api` |

`zenith diagnose` reports the first two directly — an empty store makes every document row
report its file as missing, and the lock budget is a check of its own.

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
