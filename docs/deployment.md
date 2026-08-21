# Deploying Zenith Enterprise

The production host is an OVH VPS behind Coolify's Traefik. Everything below assumes
`/srv/zenith` and access over Tailscale — port 22 is closed publicly.

```bash
cd /srv/zenith/docker
COMPOSE="sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml"
```

The overlay is applied on top of the base file, never instead of it. What it changes and
why is documented in `docker-compose.prod.yml` itself.

## What this box can run, and what it cannot

Measured on the machine, not estimated: **4 cores, 7.75 GB, ~5 GB free** after the 28
containers already on it.

| | Memory |
|---|---|
| `tei-embed` at 2048 batch tokens | 2.6 GB |
| `tei-rerank` | 5.1 GB |

The cross-encoder does not fit, so the profile is `low-spec` and **reranking is off**.
That is a visible degradation, not a hidden one: search results say `Degraded` and the
status panel lists the reranker as disabled. Recall@8 is roughly 20 points below what the
same corpus gives with reranking on.

Two ways out, both real:

- More memory. At ~14 GB the whole stack fits, `ZENITH_HARDWARE=cpu` and start `tei-rerank`.
- A smaller cross-encoder. `bge-reranker-v2-m3` is 568M parameters; a ~278M multilingual
  model fits this box *and* answers faster. This also fixes the 15-second searches measured
  on much larger hardware — the model, not the machine, is the bottleneck there.

## First deployment

```bash
$COMPOSE build api worker frontend
$COMPOSE up -d db
$COMPOSE exec api alembic upgrade head       # nothing runs migrations automatically
```

Then the two roles that ship `NOLOGIN`. No code anywhere gives them a password — that is
deliberate, so a credential never lives in the repository:

```bash
$COMPOSE exec db psql -U zenith -d zenith -c \
  "ALTER ROLE zenith_app LOGIN PASSWORD '<ZENITH_APP_PASSWORD from .env>';"
$COMPOSE exec db psql -U zenith -d zenith -c \
  "ALTER ROLE zenith_platform LOGIN PASSWORD '<ZENITH_PLATFORM_PASSWORD from .env>';"
```

Without the first, `verify_rls_active()` refuses to start rather than silently connecting as
the schema owner with row-level security bypassed. That check exists for exactly this
mistake, and a startup failure is the correct outcome.

Then the rest, and the first organisation:

```bash
$COMPOSE up -d tei-embed api worker frontend
$COMPOSE exec api uv run python -m app.cli create-tenant "<name>" <admin-email>
$COMPOSE exec api uv run python -m app.cli grant-system-admin <admin-email>
```

`grant-system-admin` is the only way to create the first one. It cannot be granted from
inside the product — migration 0010 revokes the application's UPDATE on that column — which
is the point: an organisation administrator holding every permission in the catalogue still
cannot reach `/system`.

## Routing and certificates

Traefik is Coolify's, already holding :80 and :443. The frontend joins its `coolify`
network and carries the routing labels; **nothing in this stack publishes a port**. On a
public box, a published 5432 or an unauthenticated embedding service on 8081 is the entire
attack surface.

Verify the certificate resolved before announcing anything:

```bash
curl -sI https://zenith.example.com | head -3
```

A 404 from Traefik means the labels are not being read — check the container is on the
`coolify` network. A certificate error usually means Let's Encrypt has not finished; give it
a minute rather than changing configuration.

## After changing code

Images are a build-time snapshot, not a bind mount:

```bash
$COMPOSE up -d --build api worker
```

**Rebuild `worker` too, not only `api`.** They are separate images from the same source, and
a task registered in `ingestion/tasks.py` exists only in the image that was rebuilt. A stale
worker accepts the job and fails it with `Task was not found`, which reads like a queue
problem and is a build problem. This has been repeated more than once.

If the change added a migration, apply it and *check*:

```bash
$COMPOSE exec api alembic upgrade head
$COMPOSE exec api alembic current   # must equal head, not merely "no errors"
```

A green test suite does not mean the deployed database is migrated: pytest builds a
throwaway Postgres per run and migrates it from zero. Migration 0007 passed the suite and
left every `POST /labels` answering `500 UndefinedColumn` in the running app.

## Backups

**There are none yet, and this is the largest outstanding risk on this list.** Purging an
organisation is irreversible by design; a disk failure is irreversible by accident. Until
`pg_dump` is scheduled and the document root under `backend/.data/documents` is copied
somewhere else, a lost volume is a lost customer.
