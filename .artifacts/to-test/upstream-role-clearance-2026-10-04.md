# Standalone upstream extraction: role-clearance

Base: upstream main `33b48812c95150348c52d2519159780252c92db2`.
Fork source: `37a52a4740c71c1368c91eb92c69d5ad85caa03e`.

The role form submitted priority_level zero while the unchanged API requires one to ten, so creation returned 422.

Create empty-permission roles at level one and offer only levels one to ten. Creating a role grants no labels, groups or permissions. Keep the API validation and authorization unchanged.

Integrated-source actual role creation returned 201 after the original 422; edit and deletion also passed. Component assertions verify the submitted minimum and available levels. Independent slice checks and CI are recorded separately.

No dependency on another extraction, the v10 foundation or Jev. No migration or dependency upgrade.
Publish the isolated slice and wait for its actual full backend/frontend CI.
Keep Docker/WSL closed; existing model-backed and database evidence is linked rather than described as rerun.
Upstream adoption remains the maintainer decision; do not merge or close existing upstream PRs.

## Extraction checks

Local TypeScript passed. Focused frontend: 11 assertions passed on locked upstream Vitest 2.1.9.
The PR records the final full Linux backend/frontend results at the published head.
