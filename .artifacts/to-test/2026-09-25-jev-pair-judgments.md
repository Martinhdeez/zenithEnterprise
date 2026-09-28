# Optional Jev pair judgments (PR 02)

Base: `b354b8ee29af99975bd5308d2197a9f002c512cb` (PR 01). This branch does not select Jev as the product reranker.

## Purpose and behavior

Add a strict, bounded HTTP client for TypeSafe Jev Score and Noul on one original-query/candidate pair per call. Record pinned model, rubric, full Score distribution, provider score/confidence, explicit utility map, rendered input hash, and reported usage. All external processing purposes default off. A caller must supply a current authorization callback for each candidate; a mixed or denied set is rejected before any dispatch.

Use a fixed HTTPS destination, no redirects, bounded body/depth, total deadline, per-call timeout, bounded queue/concurrency, and request/token reservation. Errors carry safe reason codes, never request or response bodies. Batch failures remain failed judgments with null ranks; later integration must choose a whole-order fallback.

## Tests and rollback

HTTP fixtures inspect exact outbound state and header behavior. Tests cover Score/Noul values, malformed distributions/types/model/IDs/usage, denied export, authorization-error sanitization, missing key, deadline/cancellation, shared queue/breaker, quota, partial batches, and no redirect follow. The ordinary TEI path must remain unchanged. A matched live pilot used only approved public material under both $0.50 and 1,000-call ceilings; its raw outputs and limitations are in `backend/eval/reports/`. Rollback disables Jev purposes and returns to the local TEI route; no schema migration is planned for this slice.
