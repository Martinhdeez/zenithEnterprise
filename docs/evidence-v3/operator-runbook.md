# Evidence retrieval operator runbook (M1)

The default remains local legacy retrieval with the installed TEI reranker. No Jev
credential, processing permission, or new database schema is required. Search can
show a hybrid candidate receipt without enabling direct mode; direct and automatic
mode controls appear only when `ZENITH_DIRECT_ENABLED=true`.

## Install and verify locally

Follow [the base installation](../deployment.md) for a real ParadeDB/PostgreSQL
database, migrations, the separate queue schema, application-role login, and TEI
services. Use a disposable Compose project, distinct host ports and volumes when
testing alongside another stack. Never run retrieval as the schema owner or
platform role. From a clean checkout, the development dependencies and local
checks are:

```bash
cd backend
uv sync --group dev
uv run ruff check .
uv run ruff format --check .
uv run pyright --pythonplatform Linux
uv run pytest -q app/features/retrieval/tests tests/integration/test_search_api.py
cd ../frontend
npm ci
npm run lint
npm run test
npm run build
cd ..
./scripts/check-licences.sh
```

The PostgreSQL tests use disposable real ParadeDB under `zenith_app` RLS, never
SQLite. On Windows, set `PYTHONUTF8=1` and use a Selector event-loop bootstrap
for pytest and Alembic subprocesses. The ordinary Windows `make check` currently
stops on the pre-existing `os.uname` Pyright error recorded in
[the baseline](phase0-baseline.md); run and record that result rather than
claiming its downstream targets passed. The licence script uses Bash. On a
Windows-only host, its equivalent PowerShell gate from `backend/` is:

```powershell
$shippedPackages = @(uv export --no-dev --no-hashes --no-emit-project | Where-Object { $_ -match '^[a-zA-Z0-9]' } | ForEach-Object { $_ -replace '[=;\[].*', '' })
uv run pip-licenses --packages $shippedPackages --fail-on='GNU General Public License v3 (GPLv3);GNU Affero General Public License v3;GNU Affero General Public License v3 or later (AGPLv3+);GNU General Public License v2 (GPLv2);Other/Proprietary License'
```

With a running local installation, authenticate with a user holding
`query.execute` and verify both `GET /search/capabilities` and
`GET /search?q=<public-query>&mode=legacy` through the frontend origin. The
development Vite proxy and deployed frontend already route `/search` to the API.
If the local API uses a port other than 8000, set `VITE_API_PROXY_TARGET` before
starting Vite; this only changes the development proxy, not the production
bundle. A small public [proxy fixture](../../frontend/e2e/public_proxy_fixture.py)
can check routing without a database: run it with Python from the repository
root, start Vite with `VITE_API_PROXY_TARGET=http://127.0.0.1:18080`, then
request `/search/capabilities` and `/search?q=public+water&mode=direct` at the
Vite origin. This scripted fixture verifies routing and UI contracts only; it
does not establish backend retrieval or model quality.
Open a returned passage and confirm its original document/page or character
range. Pending ingestion is excluded from direct receipts; first verify the
document is ready. `mode=direct` requires an enabled direct flag and a scope
within the configured budgets. `mode=auto` falls back to hybrid with a
candidate-only receipt if full direct assessment cannot complete.

## Configure experimental paths

All environment names below include the `ZENITH_` prefix from `Settings`.
Set `ZENITH_DIRECT_ENABLED=true` to expose `auto` and `direct` in Search. Direct
work defaults to 16 source units, 32 assessment windows, 48,000 source bytes,
180,000 rendered bytes, 500 bytes per question/window pair, and a 25-second
total deadline. These are workload bounds, not relevance thresholds. Direct
TEI calls refuse truncation. If an input exceeds the serving model's limit,
the receipt is incomplete; do not treat it as exhaustive. A complete receipt
means every window in the eligible *parsed, ready, authorized* source manifest
was assessed. It does not certify extraction quality, answer support, or the
entire archive beyond the selected scope.

External reranking requires an operator-approved purpose covering the complete
rendered question and source context. Use a secret mechanism for
`ZENITH_JEV_API_KEY`; never put a real key in the repository or a command log.
Only after approval, set `ZENITH_EXTERNAL_PROCESSING_FOR_RERANKING=true`,
choose `ZENITH_EVIDENCE_JUDGE_PROVIDER=jev_score6` or `jev_noul`, pin
`ZENITH_JEV_MODEL`, and set positive `ZENITH_JEV_MAX_REQUESTS` and
`ZENITH_JEV_MAX_INPUT_TOKENS`. The default zeros block paid requests. A verified
single API worker may set `ZENITH_JEV_SINGLE_WORKER_ACK=true`; multiple workers
need a shared limiter first. `ZENITH_JEV_MAX_CONCURRENCY` defaults to 2,
`ZENITH_JEV_MAX_PENDING` to 8, queue wait to 2 seconds and total Jev batch
deadline to 20 seconds. These controls do not enforce a monetary invoice cap;
approve and monitor spend separately. Reranking permission does not enable
segmentation or claim support: their distinct flags remain false unless each
purpose receives separate approval.

Keep the returned provider, effective model, rubric, utility mapping and input
identities with evaluation artifacts. Score6 utility and Noul ranking values
are not answer correctness probabilities, and a hybrid shortlist receipt is
not whole-scope coverage. A failed Jev judgment may use a complete local TEI
order; a failed local order remains degraded. The Search screen names these
coverage states without turning a missing judgment into zero relevance.

## Experimental evidence packets (PR 06)

`ZENITH_EVIDENCE_PACKETS_ENABLED=false` is the default. When enabled, answer
generation assembles cited chunks with bounded local context from ready,
currently authorized sources. The packet has a conservative 16,000 UTF-8-byte
upper bound over the system text, conversation, question, passage text, and
metadata (`ZENITH_EVIDENCE_PACKET_TOKEN_BUDGET`), plus a maximum of eight extra
chunks (`ZENITH_EVIDENCE_PACKET_MAX_EXTRA`). This is a tokenizer-independent
conservative bound, not an exact LLM usage meter. Packet work uses the
application database role and rechecks every selected chunk before inference
and disclosure. Each packet-build or final-recheck stage has a five-second
default deadline (`ZENITH_EVIDENCE_PACKET_DEADLINE_SECONDS`). Packet streaming
is buffered until the final recheck.

The selector considers local conditions, exceptions, table headers and clear
same-document section references. If a known dependency is inaccessible or
cannot fit, it omits that fact and sets `degraded=true` with
`reason=packet_dependencies_unresolved`; it does not silently send the
unqualified fact. `reason=packet_unavailable` and `packet_source_changed`
indicate that source-derived prose was withheld. These reason codes do not
expose hidden IDs or counts.

`ZENITH_EVIDENCE_COUNTEREVIDENCE_ENABLED=false` independently controls a
bounded same-document exception probe. Its default maximum is two extra
candidates (`ZENITH_EVIDENCE_COUNTEREVIDENCE_MAX_CANDIDATES`). A candidate is
shown to the generator as a *possible* exception with applicability unassessed;
the lane does not certify a conflict. Both packet modes are experimental and
remain disabled pending broader answer-quality and load validation. No new
external-processing permission or Jev key is needed because selection is local.
Disable both flags to restore the old top-eight answer context. This path adds
no migration, vector rewrite, or new citation ID.

## Experimental strict claim support (PR 07)

`ZENITH_STRICT_CLAIM_SUPPORT_ENABLED=false` remains the default. Strict mode
buffers the complete generated draft, checks citation references, quoted spans,
numeric tokens and simple arithmetic, then sends bounded claim/context pairs
to the distinct Jev claim-support purpose. A single repair is allowed and every
changed claim is reassessed before prose is released. The streamed endpoint
therefore sends no provisional answer text in strict mode. It returns
`support_status` and `took_support_ms` in the final result. A failed, timed-out,
oversized, or unauthorised assessment returns an abstention with a degraded
reason, not the unassessed draft. The caller's current source access is
rechecked under the application role before dispatch and disclosure.

Enable only after approving external processing of the **whole rendered
context** for claim support. Set `ZENITH_EXTERNAL_PROCESSING_FOR_CLAIM_SUPPORT=true`
separately from the reranking and segmentation flags, supply the secret key,
and configure a positive Jev request/token quota and topology as above. The
default strict bounds are eight clauses, 12,000 bytes per rendered
claim/context input, a 45-second total support-and-repair deadline, and a
conservative `0.8` Noul threshold. Those bounds do not imply a model-quality
guarantee. The [public SciFact trial](../../backend/eval/reports/evidence-v3-scifact-support-2026-09-26.md)
includes false suppression and is insufficient to promote strict mode by
default. The answer's normal source markers and stored citation IDs retain
their meaning. Disable `ZENITH_STRICT_CLAIM_SUPPORT_ENABLED` for immediate
flag rollback; no schema migration or index change is involved.

## Rollback

Set `ZENITH_DIRECT_ENABLED=false`,
`ZENITH_EVIDENCE_JUDGE_PROVIDER=tei`, and all three
`ZENITH_EXTERNAL_PROCESSING_FOR_*` flags to false; remove the Jev credential
through the secret mechanism. Use the default `mode=legacy` in clients or hide
the new mode control. No migration, vector rewrite, or source reindex is part
of M1. Old query history and citations retain their original provider/source
meaning; the browser's former shared recent-search key is removed, and new
recent suggestions are held in memory only and cleared on identity changes.
