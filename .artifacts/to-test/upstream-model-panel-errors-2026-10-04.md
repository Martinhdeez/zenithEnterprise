# Standalone upstream extraction: model-panel-errors

Base: upstream main `33b48812c95150348c52d2519159780252c92db2`.
Fork source: `37a52a4740c71c1368c91eb92c69d5ad85caa03e`.

A denied model-configuration read produced an uncaught promise rejection; an old request could update a panel after its identity changed.

Show the existing refusal as an alert with retry, clear identity-bound loading state and ignore late read success/failure. Keep configuration authorization and save behavior unchanged; no model calls or provider default changes.

Integrated-source denied-member browser replay produced zero page errors and preserved the 403 boundary. Component regressions verify denial, retry and stale read responses. Independent slice checks and CI are recorded separately.

No dependency on another extraction, the v10 foundation or Jev. No migration or dependency upgrade.
Publish the isolated slice and wait for its actual full backend/frontend CI.
Keep Docker/WSL closed; existing model-backed and database evidence is linked rather than described as rerun.
Upstream adoption remains the maintainer decision; do not merge or close existing upstream PRs.

## Extraction checks

Local TypeScript passed. Focused frontend: 4 assertions passed on locked upstream Vitest 2.1.9.
The PR records the final full Linux backend/frontend results at the published head.
