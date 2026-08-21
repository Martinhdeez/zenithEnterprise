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

Purging an organisation is irreversible by design; a disk failure is irreversible by
accident. Two things have to be copied, and neither is sufficient alone — the database holds
the tenants, grants, audit trail, chunks and vectors, while `backend/.data/documents` holds
the PDFs themselves. The vectors cannot be regenerated without the files, and the files mean
nothing without the rows that say who may read them.

```bash
./scripts/backup.sh                  # -> ./backups/<utc-timestamp>/
./scripts/backup.sh /mnt/elsewhere   # anywhere else
```

Each run writes `database.dump` (pg_dump custom format), `documents.tar.gz` and a
`manifest.txt`, then **verifies what it wrote**: it asks `pg_restore` to parse the archive
and counts the rows against the files. A backup nobody has read is a hope rather than a
backup.

**The database is dumped before the files, and the order is deliberate.**
`DocumentService.create` commits the row and *then* writes the PDF, so dumping the database
first means a document uploaded mid-backup is missing from the dump but present on disk — an
unreferenced file, which is harmless. The reverse order produces a row pointing at a file
that was never copied. Neither order snapshots a live system perfectly; stop `api` and
`worker` first if you need a guaranteed-consistent copy.

Restoring:

```bash
./scripts/restore.sh backups/2026-08-21T15-00-53Z
```

It stops `api` and `worker`, replaces the documents, restores the database in a single
transaction, restarts, and then checks that every document row has its file. It asks you to
type the backup's directory name first, because it destroys what is there.

**Verified rather than assumed.** The dump was restored into a scratch database and checked:
21,295 chunks and their vectors, the HNSW and tsvector indexes, 20 tables with row-level
security and their 20 policies, and — the one that matters most — `audit_events` coming back
with `INSERT, SELECT` and nothing else, so the log is still append-only after a recovery.

What is still missing: **this is not scheduled and it is not off-site.** A cron entry and a
copy to another machine are the remaining work; a backup on the disk that fails is not a
backup. `backups/` is git-ignored — it holds customer data and must never reach the
repository.
