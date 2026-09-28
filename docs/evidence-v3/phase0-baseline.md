# Evidence v3 source and baseline manifest

Reviewed upstream base: `33b48812c95150348c52d2519159780252c92db2`.
`upstream/main` resolved to the same commit on 2026-09-25. The working fork is
`Kripta-Studios/zenithEnterprise`; the upstream repository is
`Martinhdeez/zenithEnterprise`. No open pull requests were found in either
repository at discovery. Migration head is `0027_projected_embedding_spaces.py`.

The current product uses PostgreSQL/ParadeDB, dense, lexical, and exact retrieval
with RRF candidate fusion. The search service closes its application-role tenant
session before calling the TEI reranker. The current legacy relevance policy can
withhold hits when lexical share is too low. The Docker Compose reranker default
is `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`; the old Python `MODEL` constant
names BGE for offline evaluation and does not establish the model currently
serving. A deployed TEI instance needs `/info` inspection for that identity.

The source investigation also confirmed that scoped lexical retrieval applies a
global cutoff before document filtering, citations bind returned source IDs, and
query history remains scoped to its user under RLS. PR 01 does not change these
policies. Later evidence-policy and scope changes must test them explicitly.

## Reproducing the engineering baseline

From a clean checkout at the base SHA, with the repository's documented
dependencies installed, run `make check`. On the inspected Windows host,
`ruff check` and `ruff format --check` passed, then Pyright failed at the
pre-existing Linux-only `os.uname` call in `backend/eval/resources.py:131`.
The standard Makefile therefore did not reach backend tests, licenses, or web
checks. `uv run pyright --pythonplatform Linux` passed on the PR 01 tree; it is
an explicit cross-platform override, not a claim that `make check` passed.

A separate clean detached worktree at the base SHA reproduced a second
Windows limitation: `test_every_check_runs_even_when_the_database_is_unreachable`
remained pending for more than three minutes after collection, and was
interrupted. The PR 01 tree behaved the same way. This is not a passing test;
the full-suite gate remains open. The backend run that excluded the entire
diagnostics module passed 860 tests and skipped 11 in 21m40s against real
PostgreSQL. The diagnostics module had 37 tests and was not part of that run.

For isolated database checks on Windows, set `PYTHONUTF8=1` so Docker context
metadata is decoded correctly, and use a Python startup hook that selects
`WindowsSelectorEventLoopPolicy` for psycopg, including Alembic subprocesses.
The test fixture starts a disposable ParadeDB container, applies migrations,
and exercises the application role; SQLite is not equivalent. Use an isolated
environment and record the exact command, commit, result, and container
configuration for each run. Do not attach these tests to a production database.

The historical retrieval JSON and named misses in `backend/eval/` are prior
results. The original corpus and local inference services were unavailable at
this checkpoint, so no fresh quality benchmark or comparison with main was
measured for PR 01. The later comparison must use matched public source
snapshots, candidate sets, and reviewed source labels, with model, rubric,
utility map, context, budget, and latency method pinned.
