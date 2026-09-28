# Evidence v3 local release validation

This is a local acceptance and repair record for
`fix/evidence-v3-release-validation`, based additively on the preserved
`integration/evidence-v3` head
`0a24cc34b05a1b055452069849ebea61a4d9aba6`. The verified upstream and
fork `main` base was `33b48812c95150348c52d2519159780252c92db2`.
The original worktree and all historical feature branch tips were preserved.
The named `Zenith_Evidence_Fork_v3/CODEX_V3_RELEASE_VALIDATION_PROMPT.md` was
checked explicitly by path in both worktrees but was absent; the user's full
inline assignment and the present handoff specifications governed this pass.

## Implementation repairs

- Strict support now treats a failed final source check as unknown, withholds
  the draft and consulted-source metadata, and renders a distinct localized
  reason. Two regression cases failed before the repair and passed afterward.
- A Jev credential changed inside a running worker is rejected; an operator
  must restart the single worker to establish a fresh in-process quota and
  breaker. Multi-credential or distributed quota isolation is not claimed.
- Narrow browser layouts stack the search and source panels, preserving a
  usable source preview and the keyboard search-mode control.
- The public direct/hybrid pilot writes its optional report to an ignored
  temporary path. Five pinned BERT corpus cases now execute, and a metadata
  test no longer waits for an unrelated PDF. Three GDPR-only cases and two
  full-question-corpus anchor cases remain skipped because the pinned
  EUR-Lex PDFs could not be retrieved. The anchor marker now checks every
  source document it needs, so fetching only GDPR cannot falsely unskip it.
- The descriptive QASPER join now aggregates planned calls and conservative
  tokens while retaining the two separate manifest hashes and authorization
  caps. A regression test prevents inherited historical totals or a fictitious
  combined spending cap. Its saved predictions were rescored offline; the
  reported metrics did not move.
- The four simultaneous Vite origins now use separate ignored optimizer
  caches. The first experimental run exposed an `EPERM` cache rename and
  React hook errors that the old page-error-only capture missed. The harness
  records tab origin, console errors, page errors, and HTTP failures; the
  fresh fixture passed with no such events.
- A full-suite run at `fb0a35b` exposed a Windows timestamp-resolution flake
  in the checkout-write guard's own test: two equal-length writes could retain
  one observed mtime. The test now sets a distinct timestamp explicitly, while
  the guard itself and its write policy remain unchanged. Its six focused
  cases pass at `1862072`.
- The remaining 176 QASPER test papers were frozen before new dispatch,
  assessed with the historical fixed-candidate policy, and reported apart
  from the reused 240-paper result. See the
  [preregistration](../../backend/eval/reports/evidence-v3-qasper-extension-prereg-2026-09-27.md)
  and [result](../../backend/eval/reports/evidence-v3-qasper-extension-2026-09-27.md).

## Authenticated local acceptance

The actual FastAPI app, migrated disposable ParadeDB/PostgreSQL, queue worker,
application-role RLS, Vite proxy, installed Chrome, pinned local BGE-M3 and
GPU TEI were used. Provisioning used isolated owner privileges only for setup;
served requests used `zenith_app`. Synthetic fixtures included two tenants,
three alpha labels, hidden-label and hidden-tenant documents, a text source,
and a searchable three-page PDF. Uploads went through the supported API and
worker, and the tests waited for `ready`. UI login used server-issued normal
member sessions; no auth route was mocked or bypassed. Four separate API/Vite
configurations tested default flags, direct/auto/packets, strict fail-closed,
and unavailable reranking. The experimental answer generator alone used the
shipped mock model boundary; this evidence is application behavior, not live
answer quality.

`scripts/evidence_v3_acceptance/browser.py` tests unauthenticated rejection,
real UI login, default legacy/TEI and semantic-hybrid behavior, keyboard
controls, actual original-file SHA-256 downloads, exact text character range,
PDF page two and visible geometry overlay, Spanish and a 430-pixel viewport,
guessed-ID denial, logout and account switching. The protected text source is
also reached with legacy search after an authorized label scope is selected.
`experimental_browser.py` tests direct cap and cap-plus-one receipts,
automatic fallback, buffered packet answers and original source identity,
strict abstention without a released draft, unavailable-TEI degradation,
unresolved packet context, and same-tenant private history. Its strict and
packet SSE assertions require exactly one final token equal to the assessed
result. `load_check.py` made 12 concurrent, real-route searches across two
tenants with six workers and checked every returned document identity. This
small check says nothing about provider throughput, tenant scheduling
fairness, or distributed quotas.

The final fresh browser fixture identified commit
`1862072e55f3d79877334964b0e1b3a8f473a39f`. The default browser script
passed **11 checks**, and the experimental script passed **10**, both with
zero captured console errors, page errors, or HTTP failures. The latter also
checked unavailable-TEI legacy degradation. The 12-request, six-worker
real-route isolation check returned twelve 200s, zero degraded responses,
and no cross-tenant or hidden-label document IDs (median 3,178.5 ms, maximum
5,188 ms). This small fixture does not measure scheduling fairness or real
provider throughput. The screenshots were inspected: text ranges and PDF
page-two overlays were visible on desktop, and the cited PDF overlay remained
visible in the 430-pixel Spanish viewer. The strict screen showed an
abstention and no released draft. The initial failed experimental trace is
retained separately as evidence of the cache defect, not counted as a pass.
Synthetic screenshots, fixture credentials, raw logs, and browser state are
ignored under `.scratch/release-validation/`; the passing logs are
`final4-default-browser.log`, `final4-experimental-browser.log`, and
`final4-load-check.log`, each with an exit-code file. The reproducible
commands and service configuration are in the [operator runbook](operator-runbook.md).
The ignored `browser-evidence-index.json` (SHA-256
`248d6b0f6af2c36b1bed22862ffd390595629c08748494db2bf21898270aa280`)
binds the tested commit, a SHA-256
of the fresh random fixture, script hashes, passing logs, and screenshot
hashes without storing credentials in this tracked summary. The scripts
record browser errors without logging bearer tokens.

## Integrated engineering gates and skips

The historical `make check` at `0a24cc3` passed 1,003 backend tests with 15
skips, 449 frontend tests, lint, format, typing, and licences. An initial
revised-head run failed at a line-length lint error; that line was repaired.
The configured direct/TEI and Ollama tests each passed one case at `fb0a35b`;
the only later change was the checkout-guard self-test. At `1862072`,
`uv sync --group dev` checked 103 packages, clean `npm ci` installed 630,
backend wheel/sdist and frontend production builds exited 0, and
`npm audit --omit=dev` exited 0 with **zero production advisories**. The
full audit exited 1 with five development-tool advisories
(three moderate, one high, one critical) across Vitest/Vite and their tool
dependencies; a Vitest major upgrade was not forced into this patch. The
configured direct/hybrid pilot used real local TEI; the isolated local Ollama
answer/abstention smoke ran after TEI was stopped to free GPU memory. Neither
is a broad quality or load result. The first revised full-suite attempt at
`fb0a35b` had **1,012 passed, nine skipped, one failed**; its sole failure was
the timestamp-sensitive guard self-test described above. It stopped before
the licence/frontend stages. The complete `make check` at the follow-up
test-only commit `1862072e55f3d79877334964b0e1b3a8f473a39f` exited
**0**: Ruff lint and format, strict Windows Pyright, **1,013 backend passed,
nine skipped in 1,527.07 seconds**, production-package licence check on 123
resolved packages, frontend TypeScript lint, and **450 frontend passed across
43 files**. The check log and exit file are ignored as
`.scratch/release-validation/final4-make-check.*`. See the
[skip audit](skip-audit-2026-09-27.md) for all original node IDs and the nine
remaining default-suite skips after corpus repair.

The final review checks source reauthorization before dispatch and disclosure,
purpose separation for the whole rendered input, semantic routing without a
lexical veto or failed-as-zero score, direct no-truncation/manifest guards,
strict buffering and abstention, no inference under a database transaction,
bounded cancellation/queues, and private history/source links. Single-worker
in-process Jev quota is explicit; cross-tenant fairness and provider capacity
remain unqualified. A distinct read-only reviewer checked the critical code,
found and verified repairs to combined-study provenance and browser error
capture, and gave a final **ship** recommendation after inspecting the
exact-head gates and screenshots. It found no remaining material code or
reporting defect. This is not maintainer approval; maintainer review remains
outstanding.

## Documentation and project-convention audit

A documentation-only follow-up checked the v3 records against
`CONTRIBUTING.md` and `CLAUDE.md`: repository prose and commits remain in
English, the feature plans retain their `.artifacts/` workflow, integration
tests use real PostgreSQL/application-role RLS, and the v3 stack adds no
migration or active-index cutover. `docs/evidence-v3/README.md` now maps each
implemented or experimental capability to its operator instructions and
measurement report. Dated PR notes explicitly label older Windows and browser
blockers as historical. The qualification report leads with the exact current
gate and browser results; the provider guide states the single-worker
credential-rotation restriction. The runbook distinguishes an ignored local
research secret from a versioned file or production secret mechanism.

At unchanged executable/test/configuration SHA `1862072`, Ruff lint and
format, strict Pyright, frontend TypeScript lint, and `git diff --check` all
passed again. Local Markdown links were checked, and the saved browser
evidence hashes, exit files, full-gate counts, and 176/416 case-row counts
matched the tracked summaries. This follow-up changes documentation only;
the complete `make check` and authenticated browser suite remain the recorded
runs at `1862072`, not fresh runs on a different executable tree.

## Model qualification and publication boundary

The new 176-paper Jev Noul cohort had 147 evidence-addressable cases: TEI
completed 75, Jev with TEI fallback 93; paired difference +12.2 points with
95% interval +6.1 to +18.4. Binary nDCG@8 was 0.734 versus 0.887 on 132
candidate-hit cases. All 176 batches validated, reserving 1,396 new calls.
The combined 416-paper view is descriptive reuse, not an independent new
held-out experiment. TEI stays the default. Score remains experimental after
eight of 40 failed development mass validations; saved failing rows retain
failure codes but not raw response bodies, so their exact parsing cannot be
reconstructed without new unauthorized calls. R1 grouping, packets, and
strict support remain optional and unpromoted. The study does not establish
generated-answer quality, private or Spanish-domain model quality, sustained
multi-user throughput, or invoice-level spend.

An independent offline artifact audit rehashed the pinned test JSON, both
manifests and prediction ledgers; checked all 416 distinct paper IDs, selected
question IDs and question sequence, source SHA-256 values, exact candidate
source spans and deterministic IDs, TEI and Jev order permutations, and the
rendered-token cap for each scored route. It found 193 and 147 addressable
cases in the old and new cohorts, 1,916 and 1,396 reserved calls, zero paper
overlap, and a maximum rendered selection of exactly 1,100 tokens. The
historical saved-score JSON was byte-identical to offline rescoring. This
checks artifact consistency; it does not create a second held-out trial.
The corrected descriptive manifest hash is
`b3bcdad1093c4dacbd7e2d0d5e0d5c354529b068ffc786604cc9a4786e930377`;
it carries both component hashes and authorization caps, 3,312 summed planned
calls and 23,419,306 summed conservative input tokens, without inventing one
combined spending authorization.

The [publication plan](publication-plan-2026-09-27.md) preserves the PR
stack, identifies intermediate PR 05–07 proxy-guard failure, and proposes a
local backport before PR 05. A clean one-file frontend security branch was
prepared separately from upstream `main`. The fork-internal review bases
must exist in the fork; upstream contributions must target bases present in
upstream, sequentially. No push, remote PR, remote CI, maintainer approval,
merge, staging qualification, production qualification, or deployment occurred
in this local assignment.

Runtime rollback selects TEI and legacy search, disables direct, packets,
strict support and all three external-processing purposes, and restarts the
single worker after any credential change. No active-index cutover or v3
migration is needed. Code rollback follows the reviewed dependency order in
reverse; the standalone lockfile patch is independently reversible.
