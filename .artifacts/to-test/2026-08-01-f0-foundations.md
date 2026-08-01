# F0 — Foundations

**Date:** 2026-08-01
**Status:** implemented, pending validation
**Milestone:** M0
**Specification:** `.artifacts/specs/2026-07-27-mvp.md` §4, §5, §6

---

## 1. Goal

Leave the project in a state where any later feature can be written and tested. It ships no product functionality: it ships schema, isolation and automated verification.

**Exit criterion:** `make up && make migrate && make test` green against real Postgres, with both leak tests passing.

---

## 2. Scope

### 2.1 Environment

- `uv.lock` pinned, Python fixed at 3.12 across local, Docker and CI.
- `.env.example` covering every `Settings` field.
- `Makefile`: `up`, `down`, `migrate`, `revision`, `test`, `lint`, `types`, `licenses`, `check`.
- `docker compose up` brings up `db`, `tei-embed`, `tei-rerank`, `api`, `worker`.

### 2.2 Core (`app/core/`)

| Module | Contents |
|---|---|
| `config.py` | `Settings` with the `ZENITH_` prefix, including the §2.12 limits |
| `logging.py` | structlog in JSON from the first commit |
| `database.py` | `Base`, engine, session factory, `tenant_session`, `verify_rls_active` |
| `security.py` | argon2 for passwords, JWT issuing and verification |

### 2.3 Common (`app/common/`)

- `exceptions.py` — base taxonomy of domain errors.
- `repositories/base.py` — generic repository that **demands an RLS context**. Not decorative abstraction: it is the single place guaranteeing no query runs without a tenant.

### 2.4 Full schema — migration `0001`

All 17 tables from §4 of the MVP, distributed by feature under the ownership rule:

| Feature | Tables |
|---|---|
| `tenancy` | `tenants` |
| `auth` | `users`, `permissions`, `roles`, `role_permissions`, `user_roles` |
| `labels` | `access_labels`, `role_labels`, `document_labels` |
| `documents` | `documents`, `pages`, `chunks` |
| `embeddings` | `chunk_embeddings`, `embedding_spaces` |
| `query` | `queries`, `query_citations` |
| `generation` | `llm_config` |

Plus: the `vector`, `pg_search` and `pgcrypto` extensions; an HNSW index on `chunk_embeddings.embedding`; GIN on `chunks.tsv`, `chunks.label_ids` and `documents.label_ids`; and both levels of RLS policy.

> **Deliberate exception to "feature by feature".** The whole schema lands in a single migration. Foreign keys and RLS policies cross features, and a half-built schema cannot be tested for leaks — which is exactly what has to be verified before the first query is written. From `0002` onwards, every migration belongs to its feature.

### 2.5 RLS model

Two levels, enforced by the database:

| Level | Source | Rule |
|---|---|---|
| Tenant | `zenith.tenant_id` | `tenant_id = current_setting(...)` |
| Label | `zenith.label_ids` | document with no labels, or intersection with the role's |

- `zenith_current_tenant()` and `zenith_current_labels()`, both `STABLE`, read by the policies.
- The application connects as `zenith_app`, which does not own the tables and is therefore subject to the policies.
- With no context set, zero rows are visible. The failure is closed.

### 2.6 Verification

Tests live in `tests/integration/` because they cross features:

- `test_migrations.py` — `upgrade`/`downgrade` on real Postgres, plus a drift check between models and database.
- `test_rls_tenant_isolation.py` — tenant A sees nothing of B. **MVP gate.**
- `test_rls_label_isolation.py` — inside a tenant, a role without the label neither sees the document nor can infer that it exists. **MVP gate.**

Feature-specific tests live inside the feature (`app/features/<x>/tests/`). F0 has no feature logic to test yet.

### 2.7 CI

GitHub Actions: `ruff`, strict `pyright`, `pytest` with testcontainers, and a licence check that fails the build on AGPL/GPL/non-commercial.

---

## 3. Out of scope

- Functional endpoints. `GET /health` only.
- The cold-start CLI — that is M3.
- Frontend.
- Any ingestion, retrieval or generation logic.

---

## 4. Result (2026-08-01)

**Implemented and verified.** `ruff`, strict `pyright` and 12 integration tests green against real Postgres. CI green on the first run.

| Check | Result |
|---|---|
| `upgrade → downgrade → upgrade` | Reversible, no residue |
| Drift between models and database | Zero differences |
| Cross-tenant leakage (read, write, delete) | 0 cases |
| Label leakage, including inference by count | 0 cases |
| Query with no context | 0 rows — the failure is closed |
| Startup with owner credentials | Rejected |
| AGPL/GPL/non-commercial dependencies | 0 |

Two of these tests caught real defects during development, which is the proof they earn their place: the drift test found the HNSW index present in the database but absent from the models, and the migration-cycle test exposed a table count that did not discount the PostGIS tables bundled in the image.

---

## 5. Deviations from the plan

Three changes against the specification, all for the same reason: removing a silent failure mode.

**`documents.label_ids` denormalised.** The RLS policy on `documents` cannot read `document_labels` if the policy on `document_labels` reads `documents`: Postgres aborts on mutual recursion. The array is replicated, exactly as `chunks` already did.

**`chunk_embeddings.tenant_id` denormalised.** This table sits on the hot path of vector search. A policy using `EXISTS` against `chunks` would be paid per row on every query; with its own column it is a plain equality.

**No `FORCE ROW LEVEL SECURITY`.** The owner has to be able to create the first tenant from the CLI, when no context exists yet. The trade-off is that configuring the application with owner credentials would disable RLS without any symptom, so `core.database.verify_rls_active` checks it at startup and the process refuses to serve. There is a test covering it.

---

## 6. Deferred to the owning feature

- Seed the `permissions` catalogue — F2 (`auth`).
- Keep `documents.label_ids` and `chunks.label_ids` in sync with `document_labels` — F3 (`labels`).
- `pg_search` BM25 index over `chunks` — F7 (`retrieval`), where it can be measured against the `tsv` + GIN alternative.

---

## 7. Risks

| Risk | Mitigation |
|---|---|
| `pg_search` API differs from expectations | Image version pinned; the BM25 index is validated in F7, not F0 |
| testcontainers slow in CI | One instance per pytest session |
| VPS without a GPU | TEI starts on the CPU image; affects latency, not F0 |
