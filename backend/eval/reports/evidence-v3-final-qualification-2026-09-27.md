# Evidence retrieval v3: integrated qualification

## Current release-validation status

The local release-validation code/test/configuration head is
`1862072e55f3d79877334964b0e1b3a8f473a39f` on
`fix/evidence-v3-release-validation`. Its full `make check` **passed**:
1,013 backend tests passed, nine skipped, 450 frontend tests passed, and
lint, format, strict Windows typing, and the 123-package licence check passed.
Clean backend/frontend installs and builds passed; the production-only npm
audit found zero advisories. Five development-tool advisories remain.
The documentation-only report commit `17422bb1b35e1a791ac93e93744e06ac3bdd75d5`
does not change executable, test, or configuration content.

A fresh local Chrome run against the real migrated app, application-role RLS,
worker, Vite proxy, and local GPU TEI passed 11 default and 10 experimental
browser checks plus 12 real-route isolation searches at the tested code head.
Login used the real UI/server auth path; text ranges and PDF page-two source
highlights were visibly checked. Experimental answer generation alone was
scripted at the model boundary. The browser evidence index in ignored local
storage has SHA-256
`248d6b0f6af2c36b1bed22862ffd390595629c08748494db2bf21898270aa280`.
This is local application acceptance, not live generated-answer quality.

The earlier full `make check` at preserved integration head
`0a24cc34b05a1b055452069849ebea61a4d9aba6` also passed, with 1,003
backend passed/15 skipped and 449 frontend passed. The PR 08 narrative below
records historical stages, including pre-repair failures and pending gates;
those statements are superseded for the later tested head above.

The additive branch also audits conditional skips and repairs strict failure
metadata and credential-rotation handling. The
[separately frozen 176-paper Jev study](evidence-v3-qasper-extension-2026-09-27.md)
found 93/147 complete-evidence cases versus 75/147 for TEI on unused public
English QASPER test papers; the full 416-paper join is descriptive reuse.
The [tracked release-validation record](../../../docs/evidence-v3/release-validation-2026-09-27.md)
is authoritative for revised-branch commit IDs, final local gates, browser
evidence, remaining skips, and publication limits. No remote CI, maintainer
review, or staging/production qualification is implied by these local runs.

## Configuration decisions

| Configuration | Decision | Basis and limit |
|---|---|---|
| Current TEI reranking, legacy retrieval, original context | **Retain as default** | Provider-disabled behavior remains the compatibility and rollback path. |
| Jev Noul reranking on approved English research-paper text | **Keep optional** | The [locked QASPER test comparison](evidence-v3-qasper-heldout-2026-09-27.md) improved complete evidence by 11.4 points and binary nDCG@8 by 0.076, with paired intervals above zero. Eight-candidate serial reranking was about 1.96 s median versus 0.224 s for local TEI. This is fixed-candidate evidence ranking, not end-to-end answer quality or a deployment SLA. External reranking remains off by default and requires purpose-specific authorization. |
| Jev Score reranking | **Keep experimental** | In the development trial, eight of 40 Score outputs failed distribution-mass validation. Noul had a stronger valid-result record; no held-out Score promotion test ran. Failure is not zero relevance. |
| Direct scoped assessment | **Keep optional** | Bounded, authorized source manifests and honest receipts were tested; the public pilot preceded the final no-truncation guard and did not establish a broad quality gain over hybrid retrieval. |
| Structural grouping / Jev boundary proposals | **Keep experimental; leave active index unchanged** | Exact-source mapping and isolated trials are in the R1 reports. Boundary-model superiority and production resegmentation are unqualified. Deterministic grouping remains available to packets. |
| Dependency packets and bounded counterevidence | **Keep optional** | QASPER/ContractNLI source-span trials did not show a net recall improvement. Preserve the ordinary context path as rollback. |
| Strict claim support | **Keep optional, off by default** | Deterministic checks and a public real Jev assessor smoke passed; the development SciFact assessor trial had false suppression. Generated-answer correctness, latency under load, and full live support quality are not qualified. |

No model default change, production rollout, or external processing permission is proposed here. Expected utility is a ranking aid, not an answer-correctness probability. Historical receipts, source coordinates, and citations retain their actual model/source identities.

## Historical PR 08 engineering evidence

PR 01–07 are committed in dependency order and integrated locally. The PR 07 frozen backend suite passed 351 tests on disposable ParadeDB/PostgreSQL under the `zenith_app` application role, including tenant and label RLS, query-history privacy, source reauthorization, deadlines, fallback, and strict support. The full serial frontend suite passed 449 tests; Ruff, format, Linux-target Pyright, backend/frontend builds, and the production-package licence check passed. The clean detached integrated worktree installed 103 backend packages with `uv sync --group dev`, 630 frontend packages with `npm ci`, built the frontend, and passed four migration/support tests including upgrade, downgrade, and re-upgrade on disposable application-role PostgreSQL. These are actual local runs; exact commands, exit codes, and ignored logs are recorded in the local handoff `VALIDATION_RESULTS.md`.

PR 08's new application-role HTTP integration test covers public text upload, pending-to-ready ingestion, document-scoped answer, citation-linked source download, and private history. It passed once in 19.69 s with a scripted generator. A real local GPU `llama3.1:8b-instruct-q4_K_M` run on the integrated branch cited the synthetic source for an answerable query and abstained for a missing fine. The identical real-model smoke on a clean upstream-main worktree produced the same answer and abstention, so the default path remains compatible in this narrow case. Both GPU runs had TEI unavailable and used degraded local fallback; this is not a reranker-quality comparison or a statistical answer-quality result. The held-out QASPER trial above is the separate, matched comparison with the actual local GPU TEI baseline. The local Ollama and TEI model servers ran sequentially because the laptop GPU has 12 GB of VRAM.

The precommit repository-wide backend run collected 1,018 tests and ended at 1,002 passed, 15 skipped, one failed in 27m52s. The sole failure was an existing proxy guard that expected literal `"http"` values, while the configured Vite proxy uses `apiTarget`; the corresponding live Vite proxy smoke had passed earlier. The guard now extracts the actual proxy object and its three focused tests pass. The formerly hanging dead-database diagnostic is bounded to 10 seconds per check; its timeout and unreachable-database cases passed together. Production dependency licence validation passes through a tracked Windows PowerShell equivalent of the Linux shell script. The full frontend target passed all 449 tests; TypeScript lint and frontend production build, Windows Pyright, Ruff, backend wheel/sdist build, and `git diff --check` passed before commit. The full check must still be repeated on the final integrated commit; none of these individual results is labeled as that gate.

An independent post-PR 08 lockfile security slice addresses five **pre-existing** transitive production npm advisories: `fast-uri`, `hono`, `js-yaml`, `nanoid`, and `qs`. The PR 08 lockfile matched upstream main byte-for-byte. A non-major lockfile-only npm resolution passed `npm ci`, `npm audit --omit=dev` with **zero** production advisories, all 449 frontend tests, lint, and production build. Five development-tool advisories remain (including a Vitest major-version remediation); no forced major upgrade is included. The security slice changes no application source or feature default and requires its own final integrated check after stacking.

The public frontend Vite proxy smoke from PR 05 reached `/search/capabilities` and `/search` through the configured proxy. The in-app browser reported no available browser, so rendered interaction and source navigation were not visually verified. No deployed frontend proxy, production data, or production infrastructure was used. Local migration downgrade on disposable data passed; optional features use flags, with no schema or active-index change in the v3 stack. Sustained concurrent Jev throughput, mixed-tenant production load, invoice-level cost, Spanish-domain model quality, and an independently reviewed release-risk budget remain unverified. These limit promotion, not the implemented optional paths.

## Reproduction and rollback

Use the [operator runbook](../../../docs/evidence-v3/operator-runbook.md) for clean installation, PostgreSQL/application-role configuration, local TEI setup, feature flags, purpose-specific Jev policy, and rollback. On a Windows development host, set `PYTHONUTF8=1` and add the tracked `scripts/windows_selector_bootstrap` directory to `PYTHONPATH` for pytest/Alembic subprocesses. Run `uv sync --group dev` in `backend`, `npm ci` in `frontend`, then `make check` from the root against disposable PostgreSQL. The clean installation test used a separate worktree and isolated containers/volumes/ports. QASPER reproduction and immutable hashes are in the held-out report; the tracked case CSV contains no source text.

For immediate rollback, set the judge provider to `tei`, direct mode and packet/strict flags to false, and all three external-processing purposes to false. Remove the Jev credential from the secret mechanism. Historical query rows and source links should remain readable; the tested migration downgrade/re-upgrade is for disposable qualification only. If reverting commits, do it in reverse dependency order PR 08 through PR 01 rather than resetting user work. No migration or vector rewrite is required by these features.

## Historical precommit final-gate status

The final checks against the final integrated PR 08 commit, clean-install rerun, load result, and final code/security review are recorded in the local checkpoint. Do not treat an earlier PR result or a synthetic fixture as a passed final gate. Remote publication and deployment require separate authorization.
