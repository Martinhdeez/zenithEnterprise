# Standalone upstream extraction: deterministic-test-timing

Base: upstream main `33b48812c95150348c52d2519159780252c92db2`.
Fork source: `37a52a4740c71c1368c91eb92c69d5ad85caa03e`.

Coarse filesystem timestamps and loaded event-loop scheduling made the same-length-write and upload worker-order tests depend on wall-clock luck.

Explicitly set the synthetic file mtime after rewriting its original bytes and control upload completion with deferred promises. Bound Vitest to one-to-two workers. Preserve the read-only guard and production upload concurrency; no runtime behavior or dependency changes.

The original independent timing branch passed 437 frontend tests and six checkout tests and was integrated into fork main. This extraction repeats focused timing and static checks; independent full Linux CI is recorded in its PR.

No dependency on another extraction, the v10 foundation or Jev. No migration or dependency upgrade.
Publish the isolated slice and wait for its actual full backend/frontend CI.
Keep Docker/WSL closed; existing model-backed and database evidence is linked rather than described as rerun.
Upstream adoption remains the maintainer decision; do not merge or close existing upstream PRs.

## Extraction checks

Local TypeScript passed. Focused frontend: 22 assertions passed on locked upstream Vitest 2.1.9. Whole-backend Ruff lint/format and changed-file strict Pyright passed. Six checkout regressions also passed on the host.
The PR records the final full Linux backend/frontend results at the published head.
