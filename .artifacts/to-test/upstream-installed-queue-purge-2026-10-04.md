# Standalone upstream extraction: installed-queue-purge

Base: upstream main `33b48812c95150348c52d2519159780252c92db2`.
Fork source: `37a52a4740c71c1368c91eb92c69d5ad85caa03e`.

Tenant purge failed in the real worker because the restricted platform connection has no UPDATE grant on owner-installed Procrastinate queue tables.

Use the established owner connection only for cancelling the target tenant's todo queue jobs. Customer-row deletion stays on platform_session; running/failed/neighbor jobs and append-only audit grants are unchanged. Install the real queue schema in an isolated bounded database and restore global engine configuration after the regression. No grants or migrations.

The integrated installed-queue regression first reproduced InsufficientPrivilege and then passed. Production-worker replay purged the owned tenant and left all 81 neighbor job statuses unchanged. The regression also asserts retry idempotence and denied audit UPDATE/DELETE. Independent Linux CI validates this slice against real migrations and the installed queue.

No dependency on another extraction, the v10 foundation or Jev. No migration or dependency upgrade.
Publish the isolated slice and wait for its actual full backend/frontend CI.
Keep Docker/WSL closed; existing model-backed and database evidence is linked rather than described as rerun.
Upstream adoption remains the maintainer decision; do not merge or close existing upstream PRs.

## Extraction checks

Local TypeScript passed. Whole-backend Ruff lint/format and changed-file strict Pyright passed.
The PR records the final full Linux backend/frontend results at the published head.
