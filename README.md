# Zenith Enterprise

Multi-tenant retrieval-augmented search over a private document corpus. FastAPI, Postgres
(ParadeDB), React, deployed on-premise with Docker Compose.

Ask a question in plain language; get an answer where **every sentence carries a citation to a
page you can open**, or no answer at all. Isolation between customers is the product. Everything
else is negotiable.

> **Status: single-maintainer project, published for reading as much as for running.** It works,
> it is tested, and it has been installed and measured — but it has no release, no upgrade path
> between versions, and no support commitment. Treat it as a reference implementation you can
> run, not as a product you can buy.

---

## What makes it different from "chat with your PDFs"

Five invariants. Each has been broken at least once, and each now has a test that would fail.

**1. Row-level security is the only access control.** Application code never filters by tenant.
Every policy is the same template:

```sql
tenant_id = zenith_current_tenant() AND (label_ids = '{}' OR label_ids && zenith_current_labels())
```

The failure mode is inverted on purpose: a forgotten filter returns *nothing* rather than
everything. A partition inherits none of this, which is why `create_partition` builds the table,
enables RLS and installs the policy in one transaction, and why a test enumerates every partition
and fails on any that lacks either.

**2. There are exactly three ways to reach the database, and two of them bypass RLS.**
`tenant_session()` is enforced and serves every HTTP request; `owner_session()` and
`platform_session()` bypass it and are named so that grepping for them is a complete audit.
`SECURITY DEFINER` functions are a third bypass class that no grep finds, so they are enumerated
in `AUTHORISED_SECURITY_DEFINERS` — checked twice, once against what the migrations declare and
once against what a running installation actually has, because those are different questions.

**3. A tenant administrator cannot become a system administrator.** Not by policy — by a
column-level `GRANT`. The application role has no `UPDATE` on `users.is_system_admin`, so the
escalation fails in Postgres regardless of any bug in the route.

**4. The audit log is append-only at the grant level.** `UPDATE` and `DELETE` on `audit_events`
are revoked from the application role *and* from the role that bypasses RLS.

**5. Every answer carries a citation, or there is no answer.** Abstention is enforced in code
after the model has spoken, not requested in a prompt: markers naming a passage the model was not
given are stripped, and an answer left with no valid citation is discarded rather than shown.

## How it works

**Ingestion.** `pending → parsing → chunking → embedding → classifying → ready | failed`. PDFs are
parsed with layout awareness, chunked at 1,200 characters with 150 of overlap, embedded with
bge-m3 at 1024 dimensions, and stored with the normalised bounding boxes that later let a citation
highlight the exact lines it came from. A document that reaches `ready` with zero chunks is a
contradiction the pipeline refuses.

**Retrieval.** Dense HNSW, lexical, and exact-identifier matching in parallel, fused with
reciprocal rank fusion, then reranked by a cross-encoder behind a circuit breaker. The reranker is
optional in the sense that its absence degrades recall visibly and says so — not in the sense that
it does not matter.

**Generation.** Provider-agnostic: anything speaking OpenAI-compatible `/v1/chat/completions`,
including a local Ollama or vLLM. The citation gate runs after the model, in code, regardless of
which one it is.

## Quick start

Requires Docker and about 8 GB of RAM free. The reranker wants more; see `docs/deployment.md`.

```bash
cp .env.example .env       # set ZENITH_JWT_SECRET, POSTGRES_PASSWORD,
                           # ZENITH_APP_PASSWORD and ZENITH_PLATFORM_PASSWORD
make up && make up-models  # Postgres, then the embedding and reranking models
make up-app                # the API, the ingestion worker and the web client

C="docker compose --env-file .env -f docker/docker-compose.yml"
$C exec api alembic upgrade head   # nothing runs migrations automatically
$C exec api zenith install-queue   # and this is the other half of the install
$C exec db psql -U zenith -d zenith -c \
  "ALTER ROLE zenith_app LOGIN PASSWORD '<ZENITH_APP_PASSWORD>'; \
   ALTER ROLE zenith_platform LOGIN PASSWORD '<ZENITH_PLATFORM_PASSWORD>';"
$C restart api worker

$C exec api zenith create-tenant "Your org" you@example.com
$C exec api zenith grant-system-admin you@example.com
```

**`--env-file .env` is load-bearing.** Compose resolves `${VAR}` against its own directory,
so without it every interpolated variable silently keeps the development default in
`.env.example` while the API uses the real one — including both database passwords, on a
service that publishes 5432. The two roles ship `NOLOGIN` and no code ever gives them a
password, which is why the `ALTER ROLE` above is a step and not an afterthought.

`install-queue` is not optional: the job queue manages its own schema outside the migrations, and
without it the application accepts uploads and ingests none of them — 201, a row, and a status
that stays `pending` for ever. `zenith diagnose` reports this and about fifteen other things a
running installation can get wrong.

```bash
make check                              # lint + format + types + tests + licences + web. Mirrors CI.
make demo-check EMAIL=... PASSWORD=...  # is this *installation* fit to show
```

Those two answer different questions, and the second is the one that has failed in front of
people.

## Layout

```
backend/app/core/          config, the three session factories, hardware profiles, diagnostics
backend/app/features/<f>/  router, service, schemas, tests — one folder per feature
backend/alembic/versions/  sequential migrations, each with a downgrade that works
backend/eval/              read-only measurement harness; writes JSON reports to disk
frontend/src/features/<f>/ mirrors the backend features
docs/adr/                  why the expensive decisions were made, including the reversed ones
```

## On the numbers

This README quotes no recall figure, no latency and no corpus size, and that is deliberate.

Every measurement this project has taken was taken on one installation, on one corpus, on one
machine — which makes it evidence for a decision and not a property of the software. The harness
that produced them is in `backend/eval/` and it runs against your corpus, which is the only place
a number about your installation can come from. `docs/adr/` records what was measured, what it
cost, and the several occasions where the measurement contradicted the plan and the plan lost.

## Documentation

| You want | Read |
|---|---|
| Why isolation works the way it does | `docs/adr/0001-row-level-security.md` |
| Search, ranking, fusion — including a retracted design | `docs/adr/0002-hybrid-retrieval-and-fusion.md` |
| Why abstention is code and not a prompt | `docs/adr/0004-citations-enforced-in-code.md` |
| Why a component may be absent | `docs/adr/0006-circuit-breaker-for-optional-components.md` |
| Partitioning, and the lock budget it costs | `docs/adr/0009`, `docs/partitioning-modulus.md` |
| Installing, backing up, restoring | `docs/deployment.md` |
| Working on it | `CONTRIBUTING.md` |
| Reporting something dangerous | `SECURITY.md` |

## Community

- **Questions, ideas, "is this a bug or am I holding it wrong"** — GitHub Discussions. Q&A and
  show-and-tell are the two categories worth using; an answered question there is worth more than
  a closed issue nobody can find.
- **Bugs and concrete proposals** — GitHub Issues. A failing test, or the exact commands you ran
  and what came back, turns a report into a fix.
- **Security** — do not open an issue. `SECURITY.md` has the private route.
- **Pull requests** — welcome, and `CONTRIBUTING.md` is short. The two leak tests are acceptance
  gates: they do not get skipped and they do not get weakened to make a change pass.

No chat server, no forum of its own. Both would be one more place to go unanswered.

## Licence

Apache-2.0. See `LICENSE`, and `NOTICE` for the third-party components — two of which have
obligations worth reading before you deploy: the database image is AGPL-3.0, and the embedding
server is under a licence that is **not** open source and restricts offering it as a hosted paid
service.
