# Optional Jev pair provider (PR 02)

The Jev adapter is an opt-in server-side client. PR 02 does not select it in
ordinary search; that integration belongs to PR 03. With all flags at their
defaults, no document or query text is sent to TypeSafe. A credential alone
does not enable processing.

The client pins a `jev-X.Y.Z` model (default `jev-1.13.0`) and sends one
original-question/candidate-passage pair to
`https://api.typesafe.ai/v1/systemone` per call. The destination is fixed,
redirects are refused, and responses are size, shape, numeric, rubric, and
model checked. Score6 retains the full six-grade distribution, the provider's
displayed score and confidence, and a separately versioned linear grade-index
utility. Noul retains its continuous value. Neither is answer correctness
probability. Failed calls have no ranking value.

The configuration keys are `JEV_API_KEY`, `JEV_MODEL`,
`EXTERNAL_PROCESSING_FOR_RERANKING`,
`EXTERNAL_PROCESSING_FOR_SEGMENTATION`, and
`EXTERNAL_PROCESSING_FOR_CLAIM_SUPPORT`. Each purpose defaults to false and
requires separate permission for **the complete rendered request**, including
query and surrounding source context. A caller must also supply an
authorization callback; it rejects a mixed batch before dispatch and rechecks
each pair at dispatch. Product orchestration must reauthorize returned sources
before disclosure. Do not enable a purpose for merely readable private data.

`JEV_MAX_REQUESTS` and `JEV_MAX_INPUT_TOKENS` both default to zero. The client
reserves a conservative byte-based amount before each request and reconciles
with reported usage; missing usage retains the reservation. It shares one
in-process budget, queue, concurrency limit, and provider breaker across
configured clients. `JEV_MAX_CONCURRENCY` defaults to 2,
`JEV_MAX_PENDING` to 8, `JEV_QUEUE_TIMEOUT_SECONDS` to 2, and
`JEV_TOTAL_DEADLINE_SECONDS` to 20. The full batch deadline includes queueing.
The queue and breaker are provider-scoped, not generic search health. A
deployment with multiple API worker processes needs an external shared limiter
before it can safely enable this provider; until then
`JEV_SINGLE_WORKER_ACK=true` explicitly gates the factory and must only be set
for a verified one-worker topology. These settings bound requests and input
tokens, not a monetary invoice or output-token charge. Establish a separate
spend control before live operation.

Rollback is to set all `EXTERNAL_PROCESSING_FOR_*` flags false and remove the
credential through the established secret mechanism. The existing local TEI
route remains available. PR 02 adds no database migration, vector index, or
change to the default reranker.

The deterministic HTTP contract tests run with `uv run pytest -q
app/features/retrieval/tests/test_jev.py` from `backend`. The public matched
pilot and its limitations are in
[`backend/eval/reports/evidence-v3-public-2026-09-25.md`](../../backend/eval/reports/evidence-v3-public-2026-09-25.md).
