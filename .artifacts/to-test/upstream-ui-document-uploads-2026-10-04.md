# Standalone upstream extraction: ui-document-uploads

Base: upstream main `33b48812c95150348c52d2519159780252c92db2`.
Fork source: `37a52a4740c71c1368c91eb92c69d5ad85caa03e`.

A renamed TXT could select the PDF parser; clearing the native chooser could empty a deferred multi-file selection; duplicates looked like new documents and trigger-written labels were stale in upload responses.

Pass the original source filename separately from the display title; refresh only trigger-written label_ids through the same tenant-bound session. Snapshot FileList synchronously, offer existing PDF/TXT/Markdown formats, retain deduplicated status and unique recent rows. Ready duplicates say Already present; existing failed documents stay failed. Preserve streaming, byte validation, deduplication, RLS and existing ingestion.

Integrated-source actual browser/HTTP checks include 52 fresh files reaching ready, a 180-page/360-vector PDF, duplicate/source-byte validation, and fresh/changed-duplicate/quarantine label response checks. New API and component regressions cover the reported defects. Independent slice checks and Linux CI are recorded in the PR, separately from integrated-fork evidence.

No dependency on another extraction, the v10 foundation or Jev. No migration or dependency upgrade.
Publish the isolated slice and wait for its actual full backend/frontend CI.
Keep Docker/WSL closed; existing model-backed and database evidence is linked rather than described as rerun.
Upstream adoption remains the maintainer decision; do not merge or close existing upstream PRs.

## Extraction checks

Local TypeScript passed. Focused frontend: 29 assertions passed on locked upstream Vitest 2.1.9. Whole-backend Ruff lint/format and strict Linux-target Pyright passed. Native-Windows full typing also exposed upstream's unchanged os.uname reference; that baseline platform difference is retained separately.
The PR records the final full Linux backend/frontend results at the published head.
