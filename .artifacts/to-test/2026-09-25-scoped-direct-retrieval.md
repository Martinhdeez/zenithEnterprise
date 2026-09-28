# Bounded scoped direct retrieval and coverage receipts (PR 04)

Base: PR 03 `bab1578782aa26dad858ef7d258407f749e85d08`. Existing `legacy` search stays the default. Add explicit `auto`, `direct`, and `hybrid` modes. For direct/auto, resolve access and a `cap + 1` ready-chunk manifest under application-role RLS before query embedding, with source/version identities and no embedding-row dependency. Reject or fall back honestly on unit, window, byte, and deadline limits; do not truncate a huge source. Split all eligible parsed text into source-mapped overlapping windows, assess every window for complete coverage, and reauthorize the full manifest before disclosure. A complete receipt refers only to the declared eligible parsed representation, not original-document extraction quality or answer correctness.

Use local TEI or the optional authorized Jev reranking formulation. Keep one provider scale per comparable window set; on partial Jev work, use a full permitted local ordering or mark the direct execution partial. Hybrid receipts report candidate completion without pretending archive exhaustiveness. Fix the scoped BM25 cutoff by applying document scope before ranking through the existing RLS-protected tsvector query for scoped searches, without changing the security-definer function.

Test cap/cap+one, tiny scope in a larger archive, huge unit, no embedding call, missing vector, long-tail window coverage, source/hash/label changes, provider failure, honest auto fallback, and a scoped lexical regression on real PostgreSQL. Run formatting, typing, retrieval/integration checks and a constrained public direct-vs-hybrid evaluation. Rollback selects `legacy` and disables adaptive/direct use; no schema migration is expected.

## Implementation and verification, 2026-09-26

The direct planner enumerates an application-role, RLS-filtered ready-chunk manifest before
query embedding, applies source scope before the cap, and bounds units, windows, bytes,
and elapsed time. Overlapping windows keep their original chunk ID, version, and source
offsets. A second manifest read after inference detects inserted, removed, or changed
eligible material; access and labels are rechecked before dispatch and disclosure. A
complete receipt means only that every window in the eligible parsed-content manifest
was assessed. `auto` falls back to hybrid with a candidate-only receipt when direct cannot
complete. Scoped lexical search now uses the RLS-protected tsvector path before ranking,
avoiding the security-definer BM25 function's global pre-filter cutoff.

The direct feature is request-selected and `ZENITH_DIRECT_ENABLED` is off by default.
`legacy` remains the default. The API receipt has a typed, versioned response schema.
When ingestion-trimmed text prevents exact subwindow offsets, the selected hit carries
the full original chunk text and range rather than a misleading partial citation.
Direct TEI requests explicitly set `truncate=false`; the default legacy request is
unchanged. The planner caps UTF-8 pair bytes, checks the serving model's input limit,
and treats a model rejection as incomplete coverage. This matters because tokenizer
normalization can expand Unicode text beyond its original byte-based estimate.
No schema migration or production model change is included. Test-fake assessments
establish orchestration only; the separate local GPU TEI pilot and its limits are documented in
`backend/eval/reports/evidence-v3-public-2026-09-26-direct-hybrid.md`.

Validation: the focused direct/judge/search-HTTP suite passed 47 tests, and the final
frozen retrieval plus search HTTP suite passed 189 tests against disposable
PostgreSQL/application-role RLS. An earlier broad attempt hit the 25-second
production deadline on a cold/contended Windows test container; ordinary test
fixtures now use a 120-second budget, with separate short-deadline tests.
Ruff lint/format,
Linux-target full Pyright, `uv build`, and `git diff --check` passed. Standard Windows
`make check` remains blocked by the pre-existing `os.uname` Pyright error described in
`docs/evidence-v3/phase0-baseline.md`. One earlier suite run was invalidated by a
concurrent `uv build` writing into `backend/dist`; the frozen rerun passed.
