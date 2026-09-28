# Zenith v3 hardening: prepublication qualification snapshot

**Later publication state:** [the 28 September upstream submission index](upstream-submission-2026-09-28.md) records the merged fork integration and all 15 actual upstream PRs. The pre-push statements below are preserved as a dated qualification snapshot, not a current remote-status claim.

This pass starts from local release report `5cd3f9ca917a5e14a2db8d448e712c9d45d69606`, whose executable release code is `1862072e55f3d79877334964b0e1b3a8f473a39f`. Historical integration `0a24cc34b05a1b055452069849ebea61a4d9aba6` and all feature branches remain intact. A read-only remote refresh on 2026-09-28 at 13:34:36 UTC found one head on each remote: `origin/main` and `upstream/main` both `33b48812c95150348c52d2519159780252c92db2`; neither remote has a release branch. The latest v3 release remained local. At this qualification snapshot, no remote ref had been written; subsequent fork PR status belongs to GitHub.

The historical authenticated acceptance covered real login, application-role PostgreSQL/RLS, source ingest and original download, text/PDF highlights, tenant and label checks. The local lanes passed again at exact R1-free candidate `4092f2735d30e7bc360bce88113c246eaf1fda60` and now at final v10 head `910bc125bb6f731ca681f20ac8ae19d97a5e0874`: 11 default and 10 experimental checks, with 12 concurrent local searches all HTTP 200 and no degradation at each candidate. The experimental generator boundary was scripted and Jev was off in both browser rounds. See [release validation](../evidence-v3/release-validation-2026-09-27.md) and [current matrix](live-e2e-matrix.md). Historical and new evidence indexes are ignored locally and tied to their respective SHAs.

QASPER consists of real papers and human evidence labels. The old 240-paper and later disjoint 176-paper cohorts have both been inspected. The later 147 addressable cases favored Jev Noul over TEI for complete evidence, 93 versus 75 (+12.2 points, paper-bootstrap 95% interval +6.1 to +18.4). The descriptive 416-paper combination is not a new locked test. The old ledgers remain frozen. See [QASPER extension](../../backend/eval/reports/evidence-v3-qasper-extension-2026-09-27.md).

## Workstream state

| Workstream | Current disposition |
| --- | --- |
| Proxy guard and neutral evaluation helpers | Code repaired and placed in local prerequisite slices before dependent PRs. Both prerequisite exact-tip Windows gates passed. |
| Score | Raw public diagnostic captured one new 0.99-mass response; strict typed failure retained. Old payloads remain unavailable. |
| Strict support | Deterministic numeric equivalence and failure-layer trace repaired. A frozen public HealthVer transfer used disjoint development and test claims, but its broad relation labels did not establish the stricter whole-claim contract; generated-answer calibration remains unqualified. |
| R1 and packets | Existing human-label results preserved. A frozen public Spanish/English transfer reindexed structural chunks and compared flat, structural, and packet variants at equal 512-token budgets; all achieved 24/24 top-1, so benefit remained inconclusive at a ceiling. Optional, default off. |
| Jev operations | Bounded concurrency and one known-rate/overload retry added; single-worker guard retained. Sustained capacity and distributed coordination unqualified. |
| Frontend tooling | Vitest 4 targeted upgrade and two-worker test setting; final build/audit/browser gates recorded separately. |
| Publication | Local review series v10 materialized through `910bc125bb6f731ca681f20ac8ae19d97a5e0874` (tree `93a088dbf604811b938e5e73b18566937d200626`), with R1 separate. Its first review slice combines the production lockfile patch, Windows licence/frontend gates, bounded diagnostics, and shell line endings before PR01. PR06 keeps its import-order correction in PR06. All 13 exact-tip Windows gates passed after recorded retries at PR05 and the final slice. Final Windows and Linux `make check` and Windows authenticated browser passed. At this snapshot, no push, PR, fork-main advancement, default change, or deployment had occurred. |

## Dependency sketch

`upstream main -> security/Windows/diagnostics foundation -> PR 01-04 -> proxy prerequisite -> PR 05 -> neutral evaluation helpers -> PR 06-08 -> release validation -> hardening`. Optional R1 branches from an earlier neutral prerequisite and is preserved separately. The graph is materialized locally; exact-tip validation is recorded per slice and does not qualify hypothetical remote merge refs. Runtime code does not import `backend/eval`; packet evaluation obtains shared scoring from neutral modules rather than R1 trial modules.

## Local validation commands

From the repository root on Windows, use `PYTHONUTF8=1` and the existing `scripts/windows_selector_bootstrap` Python path for real PostgreSQL tests, with a disposable `ZENITH_JWT_SECRET`. Run `make check` for backend/frontend/static/license gates, `npm ci` in `frontend`, then `npm run build` and `npm audit`. The final exact test results and conditional skips are recorded in [per-PR validation](per-pr-validation.md) and [conditional tests](conditional-tests.md).
