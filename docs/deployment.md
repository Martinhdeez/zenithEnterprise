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
$COMPOSE exec api zenith install-queue       # and this is the other half of the install
```

**Both, not just the first.** Procrastinate manages its own schema, so the job-queue tables
are deliberately outside our migrations — and an installation that runs only `alembic upgrade
head` accepts uploads and ingests none of them. `POST /documents` answers 201, the row
appears, and the status stays `pending` for ever. `zenith diagnose` checks for the tables, so
the omission is reported rather than discovered.

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

If the change added a migration, apply it and *check* — and **rebuild before you migrate.**
Migration 0026 makes `query_citations.tenant_id` `NOT NULL`, so a pre-0026 image against a
post-0026 database starts cleanly and then fails on the first citation write. Settings, then
code, then schema, then statistics:

```bash
$COMPOSE up -d --force-recreate db   # only if the compose file changed; see below
$COMPOSE up -d --build api worker frontend
$COMPOSE exec api alembic upgrade head
$COMPOSE exec api alembic current    # must equal head, not merely "no errors"
$COMPOSE exec db psql -U zenith -d zenith -c "ANALYZE chunks; ANALYZE chunk_embeddings;"
```

**The `ANALYZE` is part of the migration, not hygiene after it.** 0026 partitions `chunks` and
`chunk_embeddings` and does not analyse the partitions it creates, so until autovacuum has
reached all of them the planner's estimates cover a fraction of the corpus. `zenith diagnose`
warns until it is run: a modulus sized on part of the corpus is worse than none.

**`max_locks_per_transaction` needs the `db` container RECREATED, not restarted.** It is a
start-up flag on the container's `command:` in the base compose file — 2,560, sized against
the partition count — so `$COMPOSE restart db` restarts the container that already exists with
the value it already had, silently. Left at Postgres's default of 64 the cluster has 6,400
lock slots against 1,161 for a single dense search, and beyond a handful of concurrent
searches `out of shared memory` is raised *during planning*: an HTTP 500 on an ordinary
search, not a slow answer. `zenith diagnose` reports the value actually in force, which is the
only way to tell a correct value in the repository from a correct value in the running
database.

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

Each run writes `database.dump` (pg_dump custom format), `roles.sql`, `documents.tar.gz` and a
`manifest.txt`, then **verifies what it wrote**: it asks `pg_restore` to parse the archive,
counts the rows against the files, and refuses to finish if `zenith_app` is missing from the
roles file. A backup nobody has read is a hope rather than a backup.

**Why `roles.sql` is a separate file.** Roles are cluster-wide, so `pg_dump` of a database
cannot contain them — while the dump it produces is full of `GRANT … TO zenith_app`. Restoring
into a cluster that lacks those roles therefore fails on the first grant, and because the
restore runs in one transaction, that single error discards the whole recovery. Restoring into
the cluster the backup came from hides this completely: the roles are already there.

Old runs are pruned to the newest `ZENITH_BACKUP_KEEP` (default 7, `0` disables). Each run is
the size of the whole installation, so without a bound a scheduled job eventually fills the
disk — a worse outcome than the loss it was guarding against. Only directories this script
wrote are eligible: the timestamp shape *and* a `manifest.txt` inside, because a backup
destination is usually a shared disk.

The script warns when it has written to the same disk as the installation. That copy survives
a bad migration and a dropped table; it does not survive the disk.

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

It stops `api` and `worker`, replaces the documents, **creates the roles**, restores the
database in a single transaction, restarts, and then checks that every document row has its
file. It asks you to type the backup's directory name first, because it destroys what is
there. If it cannot create the roles it stops before touching the database, leaving the
current one intact rather than half-replaced.

**Verified on hardware that had never seen this installation.** On 25 August a Docker Desktop
reset discarded the database volume, which turned the exercise into a real recovery and is
worth recording precisely:

| | |
|---|---|
| Backup of 21 Aug, restored into a virgin cluster | **fails** — `role "zenith_app" does not exist`, 0 rows |
| Same backup with `roles.sql`, same virgin cluster | 52 documents, 21,295 chunks and vectors, alembic `0016`, 20 tables with RLS |

(The counts are what the installation held that day. Two tenants of M0 leftovers were removed on 26 August; it now holds 26 documents and 8,273 passages.)
| `zenith_app` connecting with no tenant set | sees 0 rows — RLS still closed after recovery |

The earlier note in this file said the restore had been verified. It had — into a scratch
*database* inside the cluster it came from, where the roles already existed. That is the only
place the defect is invisible.

### Scheduling it

```bash
./scripts/schedule-backup.sh install /path/on/other/hardware
./scripts/schedule-backup.sh status
```

A correct backup nobody runs is the same as no backup on the morning it is needed. On 25
August a Docker reset discarded the database volume and what saved the installation was a copy
somebody had taken by hand four days earlier — luck with a shell script attached.

Daily at 03:00, because `pg_dump` and the embedder compete for the same cores and F11 measured
ingestion quadrupling query latency: a backup at nine in the morning is a backup that makes the
product look slow. `RunAtLoad` is deliberately absent, so a laptop waking from sleep does not
start a full backup on top of whatever its owner came back to do.

macOS installs a `launchd` agent. Anything else gets the crontab line printed for review rather
than written — a script that edits a server's crontab unasked is a script with opinions about
somebody else's machine.

### What is still missing

**Off-site.** Scheduling without a destination on other hardware guarantees only that you will
never forget to make a backup that a disk failure takes with it, and the script says so every
time it runs. Pass a path on other hardware to `schedule-backup.sh install` and both halves are
done.

The destination is a decision rather than a default, and deliberately so: a backup carries user
password hashes, the SCRAM verifiers in `roles.sql`, and every document of every tenant. Where
that copy lives is the customer's call, not this repository's. `backups/` is git-ignored for
the same reason — it must never reach the repository.
