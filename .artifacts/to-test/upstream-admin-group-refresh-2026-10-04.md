# Standalone upstream extraction: admin-group-refresh

Base: upstream main `33b48812c95150348c52d2519159780252c92db2`.
Fork source: `37a52a4740c71c1368c91eb92c69d5ad85caa03e`.

Creating or deleting groups left Access matrix and People and groups stale; reloads could discard other people's drafts and a deleted-group draft could be applied to the next selection.

Propagate a catalog revision, reload the affected panels, associate access drafts with their group, prune deleted group IDs, preserve valid unsaved membership edits and ignore late catalog responses. Preserve server permission checks and existing role/label rules.

Integrated-source actual browser replay passed seven checks, including real creations/deletions, refreshed catalogs, preserved drafts and removed-group safety, with zero page errors. Component regressions cover those boundaries; independent slice checks and CI are recorded separately.

No dependency on another extraction, the v10 foundation or Jev. No migration or dependency upgrade.
Publish the isolated slice and wait for its actual full backend/frontend CI.
Keep Docker/WSL closed; existing model-backed and database evidence is linked rather than described as rerun.
Upstream adoption remains the maintainer decision; do not merge or close existing upstream PRs.

## Extraction checks

Local TypeScript passed. Focused frontend: 47 assertions passed on locked upstream Vitest 2.1.9.
The PR records the final full Linux backend/frontend results at the published head.
