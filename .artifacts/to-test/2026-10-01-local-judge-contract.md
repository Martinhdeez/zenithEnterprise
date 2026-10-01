# Local judge contract

Base: freshly fetched Martinhdeez main, 33b48812c95150348c52d2519159780252c92db2.
Replaces the logical contribution of upstream #2 without its foundation dependency.

The search service consumes a provider-neutral candidate/assessment protocol through the
local TEI adapter. Candidate identities, repeated texts, batching, incoming tie order,
source coordinates, circuit breaking and fused fallback are retained. Invalid or incomplete
responses fail the whole assessment. Cancellation propagates and one deadline bounds all
sequential rerank batches. No schema, API response, dependency, endpoint or default changes.

Validation: legacy versus adapted requests/order, malformed scores/indexes, missing
identities, whole-batch fallback, ties, total deadline and cancellation. Existing real
ParadeDB search/isolation tests and complete independent CI checks are recorded separately
on perf/local-first-baseline. Historical v10 results are not this branch's evidence.
