# Evidence judge contract (PR 01)

Base: `33b48812c95150348c52d2519159780252c92db2` on `upstream/main`.

## Purpose and scope

Keep the current hybrid retrieval, RLS reads, legacy relevance bands, generation, and citation
binding while giving the existing local reranker a provider-neutral candidate assessment
contract. This slice adds no external provider, schema migration, new processing permission,
or default-model change.

## Model and business rules

An assessment names every requested chunk exactly once. A successful TEI judgment retains
the raw ranking value, source identity, rendered-input fingerprint, and serving model when
`/info` reports it. Unknown model identity remains null. Provider-specific probability,
distribution, and confidence fields remain null for TEI. Invalid, duplicate, missing, or
oversized TEI responses fail the whole batch, allowing the existing fused-order fallback.
The overall monotonic deadline covers sequential batches and identity discovery; cancellation
stops later dispatch. Duplicate rendered text is scored once per request, then mapped back to
each original candidate. Ties keep incoming fused order.

The search response adds nullable judge provider/model/score-kind metadata. The existing
`rerank_score` and citation score persist with their prior meanings. No private query or
source text is written into the new metadata. Retrieval closes its tenant session before
assessment; the adapter has no database access.

## Endpoints and checks

The existing `GET /search` gains only optional fields. Tests cover valid ordering and source
IDs, malformed output, total deadline, cancellation, deterministic fake behavior, fallback,
RLS tenant/label isolation, and history privacy. Run repository lint, formatting, strict
typing, unit/integration tests, licenses, and frontend checks where this Windows environment
supports them. The real-database tests use the repository's ParadeDB testcontainer and
`zenith_app` role.

## Rollback

Revert the adapter wiring and optional response metadata. No migration or data backfill is
needed; the existing TEI `rank()` path and fused-order fallback remain available.
