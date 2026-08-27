# Zenith Enterprise

Multi-tenant RAG over a private document corpus. FastAPI + Postgres (ParadeDB) + React,
deployed on-premise. Isolation between customers is the product; everything else is
negotiable.

Process — branching, definition of done, commit format — is in `CONTRIBUTING.md` and is not
repeated here. **The repository is written in English without exception**, including comments,
commit messages and this file.

---

## Read before you touch

| You are about to | Read first |
|---|---|
| Anything touching tenants, labels or access | `docs/adr/0001-row-level-security.md` |
| Search, ranking, fusion | `docs/adr/0002-hybrid-retrieval-and-fusion.md` |
| Generation, prompts, providers | `docs/adr/0003`, `0004-citations-enforced-in-code.md` |
| Anything whose cost depends on the machine | `docs/adr/0005-hardware-profiles.md` |
| A component that can be absent | `docs/adr/0006-circuit-breaker-for-optional-components.md` |
| Deploy, backup, restore | `docs/deployment.md` |
| Why a decision was made at all | `.artifacts/specs/2026-07-27-technical-decisions.md` |

`.artifacts/specs/2026-07-27-mvp.md` is the original specification. It is history, not
instruction: parts of it were superseded by measurement and the corrections live in
`.artifacts/tested/`. **When the spec and a document in `.artifacts/tested/` disagree, the
measurement wins.**

Working notes move `backlog → todo → in-progress → to-test → tested → shipped`. Specs never
move.

---

## Invariants

Five things. Each has been broken at least once, and each has a test that would now fail.

**1. RLS is the only access control.** Application code never filters by tenant. Every
policy is the same template:

```sql
tenant_id = zenith_current_tenant() AND (label_ids = '{}' OR label_ids && zenith_current_labels())
```

The failure mode is inverted on purpose: a forgotten filter returns *nothing* rather than
everything. If you find yourself adding `WHERE tenant_id = ...` in Python, the bug is
somewhere else.

**2. There are exactly three ways to reach the database, and two of them bypass RLS.**

| Factory | RLS | Who may use it |
|---|---|---|
| `tenant_session()` | enforced | everything served over HTTP |
| `owner_session()` | bypassed | CLI, diagnostics, ingestion requeue, tenancy provisioning |
| `platform_session()` | bypassed | `/system` routes and audit writes only |

The names are kept greppable deliberately: **grepping for `owner_session` and
`platform_session` is a complete audit of the bypass surface.** Do not wrap them, alias them,
or pass a session down through a helper that hides which one it is. `unscoped_session()` is a
fourth, narrower case — login, before a tenant is known.

**3. A tenant admin cannot become a system admin.** `users.is_system_admin` is not in the
permission catalogue, because tenant admins edit `role_permissions` freely. It is protected
by a column-level `GRANT`: the application role has no `UPDATE` on it, so the escalation
fails in Postgres regardless of any bug in the route.

**4. The audit log is append-only at the grant level.** `UPDATE` and `DELETE` on
`audit_events` are revoked from `zenith_app` *and* from `zenith_platform` — so even the role
that bypasses RLS cannot rewrite the record. This is demonstrable in one command and it is
often what decides an enterprise sale; the runbook has it.

**5. Every answer carries a citation, or there is no answer.** Abstention is enforced in
code, not requested in a prompt.

---

## Layout

```
backend/app/
  core/          config, database (the three session factories), hardware profiles, diagnostics
  features/<f>/  router, service, schemas, tests — one folder per feature, tests inside it
  cli.py         zenith <command>: tenants, users, passwords, diagnose
backend/alembic/versions/   0001..0022, sequential, each with a downgrade that works
backend/tests/integration/  only tests that cross features (isolation, RLS, BM25)
backend/eval/               read-only measurement harness; writes JSON reports to disk
frontend/src/
  features/<f>/  mirrors the backend features
  shared/        api client, ui primitives, lib
  styles/index.css   the whole theme: tokens, type roles, the surface ladder
docker/          compose, nginx; scripts/ holds demo-check, backup, licences
```

---

## Commands

```bash
make check                              # lint + format + types + tests + licences + web. Mirrors CI.
make up && make up-models               # Postgres, then the embedding and reranking models
make migrate                            # alembic upgrade head
make demo-check EMAIL=... PASSWORD=...  # is this *installation* fit to show
```

`make check` says the code is correct. `demo-check` says the running installation is correct.
They are different questions and the second one is the one that has failed in front of people.

---

## Traps that have already cost time

- **A new API prefix must be added to two proxy lists** — `docker/nginx.frontend.conf` and
  `frontend/vite.config.ts`. A missing prefix does not 404: it falls through to the SPA and
  returns `index.html`, so the client reports a parse error with the backend perfectly
  healthy. Tests never catch it, because they call the API directly. This has happened twice.
- **A green suite does not mean the database is migrated.** Check `alembic current == head`
  against the running installation. A missing migration is a 500 in production with CI green.
- **The product degrades rather than fails.** A missing reranker still answers, from the fused
  order, roughly fifteen points of recall worse and without saying so. Start `tei-rerank`.
- **Delete the row, then the file, never the reverse.** The worst case is an orphaned blob;
  the unacceptable case is a row pointing at a file that is gone.
- **Storage is partitioned by tenant** (`root/<tenant_id>/<sha256>.pdf`), so blobs are never
  shared between tenants and a tenant deletion is one path operation. Do not add reference
  counting — it would imply sharing that does not exist.
- **Numbers in documentation rot.** Every figure quoted anywhere must come from a run written
  to disk under `backend/eval/`. Two stale corpus statistics survived in the runbook for weeks
  because nobody re-derived them.
