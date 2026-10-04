# Standalone upstream extraction: spanish-credential-feedback

Base: upstream main `33b48812c95150348c52d2519159780252c92db2`.
Fork source: `37a52a4740c71c1368c91eb92c69d5ad85caa03e`.

Session and credential-link security explanations stayed English in Spanish mode; client validation was not announced and the account email lacked separating whitespace.

Translate the existing warnings and validation, update locale-sensitive callbacks, expose validation as an alert and separate the account email. Preserve minimum password length, one-use links and the documented stateless-token expiry behavior.

Integrated-source authentication/navigation and affected Spanish browser replays passed; new Spanish invitation tests assert localized help/errors and no credential consumption on client validation. Independent slice checks and CI are recorded separately.

No dependency on another extraction, the v10 foundation or Jev. No migration or dependency upgrade.
Publish the isolated slice and wait for its actual full backend/frontend CI.
Keep Docker/WSL closed; existing model-backed and database evidence is linked rather than described as rerun.
Upstream adoption remains the maintainer decision; do not merge or close existing upstream PRs.

## Extraction checks

Local TypeScript passed. Focused frontend: 9 assertions passed on locked upstream Vitest 2.1.9.
The PR records the final full Linux backend/frontend results at the published head.
