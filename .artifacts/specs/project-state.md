# Project state — Zenith Enterprise

**Living document. Updated at the end of every feature.**
Last updated: 2026-08-01 (F2 + hardening)

This file exists so that no context lives only in a conversation. If you come back
to this project in three months, or a new person joins, this is the file to read
first — then the three specs, then the code.

---

## 1. Where things stand

| Item | State |
|---|---|
| Repository | `github.com/Martinhdeez/zenithEnterprise` (private) |
| **F0 — Foundations** | **Done.** 6 commits on `main`, CI green |
| Specs translated to English | Done, `db4d762` |
| `CONTRIBUTING.md` | Merged, PR #1 |
| **F1 — Tenancy** | **Done.** Merged, PR #2, CI green |
| **F2 — Auth** | **Done.** Merged, PR #3, CI green |
| **F2 hardening** | **In progress**, branch `fix/f2-hardening` |
| F3 onwards | Not started |

### What F0 actually delivered, verified

- 17-table schema in migration `0001`, with both RLS levels enforced by the database.
- `upgrade → downgrade → upgrade` proven reversible.
- Drift test: models and database cannot diverge silently.
- Cross-tenant leak: 0 cases (read, write, delete).
- Cross-label leak: 0 cases, including inference by row count.
- Startup refuses to serve if RLS is inactive.
- CI: ruff, strict pyright, pytest on real Postgres, licence check.

`make check` runs the exact CI job. Green locally means green in CI.

---

## 2. Feature build order

Each feature retires the next dependency, not the flashiest UI.

| # | Feature | Status |
|---|---|---|
| F0 | Foundations — schema, RLS, CI | **Done** |
| F1 | `tenancy` — context and session contract | **Done** |
| F2 | `auth` — users, roles, permissions, login | **Done** |
| F3 | `labels` — access labels, the second RLS level | |
| F4 | `documents` — upload, deduplication, deletion | |
| F5 | `ingestion` — per-page parsing, chunking, bboxes | |
| F6 | `embeddings` — TEI, vector spaces, reindexing | |
| F7 | `retrieval` — BM25 + vector + RRF + re-ranking | |
| F8 | `eval` — **the M0 baseline is measured here** | |
| F9 | `generation` + `query` — connector, citations | |

---

## 3. F1 — delivered

`TenantContext` (frozen, labels as a tuple), `tenant_session(context)` as the only
supported entry point, `owner_session()` as the named and greppable RLS bypass,
`TenantService` for the cold-start path, and `ScopedRepository` as the base class.

27 tests, up from 12. Full record in `.artifacts/to-test/2026-08-01-f1-tenancy.md`.

**Proven, not assumed:** `set_config(..., true)` is transaction-local under a real
connection pool. Three tests cover concurrent contexts, sequential requests forced
onto one connection, and a context-less session following one that had a context.

**Two defects found and fixed**, both of the same class — a green suite that is not
running your tests looks identical to one that is:

- The repository guard looked for `tenant_id` while the session recorded `context`,
  so it would have rejected every *valid* session. No test built a repository, so the
  suite was green on both sides of the bug.
- Feature tests under `app/features/` were never collected: `testpaths` was `["tests"]`
  and fixtures lived in `tests/conftest.py`. Every feature test from F2 onwards would
  have silently not existed.

---

## 3b. F2 — delivered

Login, refresh, `/auth/me`, the permission catalogue in migration `0002`, and
`requires(...)` as a dependency rather than a check inside handlers.

78 tests, up from 27. Full record in `.artifacts/to-test/2026-08-02-f2-auth.md`.

**The assertion that matters:** the context built from a token carries exactly the
labels of the user's roles. RLS enforces whatever context it is handed, so a correct
policy fed a wrong context produces a confident, silent leak. Verified by sabotage —
widening the resolution to every label in the tenant fails two tests; removing the
permission check fails a third.

**Scope pulled forward on review.** Login rate limiting moved from M4 into F2: argon2
is expensive by design and login deliberately hashes even for unknown addresses, so
without a limit the two combine into a denial of service anyone can trigger. Hashing
also moved off the event loop.

**Three problems found after F2 was already green**, all of the class "the tests pass
and the product still cannot be installed":

- `ZENITH_JWT_SECRET` had a working default and nothing refused to start with it. A
  forged token defeats every guarantee F1 and F2 built. Now no default, with a
  validator and `zenith generate-secret`.
- `pyproject.toml` declared `zenith = "app.cli:app"` and `app/cli.py` did not exist.
  F2 had shipped a login for users that nothing could create.
- `zenith_authenticate_lookup` — a new RLS bypass surface — reached only the feature
  note, not `technical-decisions.md`. Now recorded in §5.1 with the complete list of
  the three bypass routes.

The CLI also exposed a real defect: engines were never disposed, so a process running
more than one event loop inherited connections bound to a loop that no longer existed.
`dispose_engines()` fixes it.

---

## 4. Decisions that are settled

Recorded so they are not relitigated. Full reasoning lives in the three specs.

### Architecture

- **Fixed local AI** for embeddings, transcription and re-ranking. **Pluggable connector
  only for final answer generation.** Variable cost scales with queries, not corpus size.
- **Postgres for everything**: pgvector, pg_search, procrastinate queue, RLS. No Redis.
  Every extra service is a support call in an on-premise deployment.
- **RLS enforces isolation**, not `WHERE` clauses. Two levels: tenant and access label.
- **Hot reindexing from day one** (RNF-08): several vector spaces coexist, so changing
  the embedding model costs GPU hours instead of a rewrite.
- **Bounding boxes, not character offsets**, for citation highlighting.
- **The RLS bypass surface is three named routes** and no more: `owner_session()`,
  `zenith_authenticate_lookup`, and the owner's credentials. See technical-decisions §5.1.
- **Secrets have no defaults.** The process refuses to start rather than warn.

### Product

- MVP installs at 2-3 design partners. No self-service signup, no billing.
- English only. PDF only. No audio (iteration 2), no conversational memory.
- RBAC is configurable: the administrator invents roles; the software defines the
  permission catalogue.
- Physical delete, accepted risk with mitigations. Soft delete is iteration 3.

### Engineering

- Everything in the repository is in English, including commits.
- Feature-based layout. No layer directories. The connector is `generation/`, never
  `openai/`.
- Real Postgres in tests, never SQLite.
- Strict `pyright`. Licence check fails the build on AGPL/GPL/non-commercial.
- GitHub Flow, short branches, rebase-and-merge, no squashing.

### Fixed limits (mvp.md §2.12)

100 MB/file · 5,000 documents/tenant · 3,000 pages/document · 30 queries/min/user ·
120 queries/min/tenant · 1 concurrent ingestion/tenant. Revisited after M0.

---

## 5. Open questions

1. **Design partners and deployment mode** — TBD, pending a commercial conversation.
   Development runs on the user's own VPS meanwhile.
2. **Support role** — does it exist, and does it see customer content or only state
   and logs? Recommendation: never content, in writing. Closes in M3.
3. **Manual upload sufficient for the MVP?** The first customer will ask for connectors.

---

## 6. Deferred work, with its owner

| Item | Lands in |
|---|---|
| ~~Seed the `permissions` catalogue~~ | Done in F2, migration `0002` |
| Keep `documents.label_ids` / `chunks.label_ids` in sync with `document_labels` | F3 |
| `pg_search` BM25 index over `chunks` | F7 |
| Wire `verify_rls_active` into the worker entrypoint | F5 (worker does not exist yet) |
| **Partitioning `chunk_embeddings` by `tenant_id` — measure in M0** | F8, see §7 |
| ~~`zenith` CLI~~ | Done in F2 hardening, earlier than planned |
| Anti-lockout guard (three routes, including deleting the last admin) | M3, with role/user management |
| Immediate revocation cache (`LISTEN`/`NOTIFY`) | M4, once a logout flow exists to trigger it |
| Rate limiter shared across processes and nodes | M4, with the §2.12 limits |
| Startup guard for `ZENITH_ENCRYPTION_KEY` | F9, when the connector reads it |
| Per-device session revocation (sessions table) | Not scheduled — see F2 §2.4 |
| Upgrade `actions/checkout@v4` and `setup-uv@v5` (Node 20 deprecation warning) | Any chore branch |

---

## 7. Per-tenant partitioning — evaluated, deferred to measurement

**The proposal:** one database per tenant, so vector search runs against a smaller
index with no tenant filter.

**Why it was not adopted:** the performance risk is not tenant filtering, it is
**label filtering inside a tenant** (RF-04.2). A role reaching 5% of its own company's
corpus degrades HNSW exactly the same way. Separating databases removes one filter and
leaves the one that actually hurts. It would also cost: pgbouncer (the extra service we
rejected twice), loss of RNF-05 idempotency because Postgres has no cross-database
transactions, N-times migrations with partial-failure states, and reindexing becoming a
coordinated operation across N databases.

**The alternative that keeps the benefit:** declarative partitioning of
`chunk_embeddings` by `tenant_id`, with one HNSW index per partition. A query with a
`tenant_id` predicate prunes to a single partition and searches only that tenant's
index — physically the same property as a separate database, with one connection pool,
one queue, one migration and RLS unchanged.

**Decision: measure before committing.** M0 already measures Recall@50 and p95 with the
user reaching 100%, 20% and 5% of the corpus. **Partitioned-versus-filtered is added to
that comparison**, so the choice is made on numbers in week four with an empty database,
not on intuition with customers in production.

This is a migration, not a rewrite. The decision stays cheap and open.
