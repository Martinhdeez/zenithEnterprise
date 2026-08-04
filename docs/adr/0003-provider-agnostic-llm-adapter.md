# ADR 0003 — The LLM boundary is an ABC in the domain layer

**Status:** Accepted

## Context

This product is sold on-premise and maintained for years. Customers will point it at
Ollama, vLLM, a corporate gateway, or a cloud provider, and will change their minds.
Swapping the model must be a **configuration change, never a refactor** — which holds only
if no vendor type reaches the application layer.

That is a layering rule, and layering rules survive exactly as long as the first person in
a hurry, unless something enforces them.

## Decision

`app/common/llm.py` holds the contract: `BaseLLMProvider`, `GenerationResponse`,
`ChunkCitation`, `GenerationUnavailableError`. Adapters live in
`features/generation/adapters/` and are the only modules permitted to know a vendor's JSON.

**An ABC, not a `Protocol`.** A Protocol is checked at the call site, so an adapter that
drifts fails wherever it happens to be used — or not at all, if the drift widens a return
type. The ABC fails at construction, in the adapter's own tests, where the person writing
it is looking.

**Errors are translated too.** An `httpx.ConnectError` reaching the router is a vendor
detail crossing the boundary as surely as a response object, and it arrives as a 500.

**Two implementations ship.** `OpenAIProvider` and `MockProvider`. An abstraction with one
implementation is a guess; nothing proves the interface is general until something other
than an HTTP client satisfies it. The generation tests use the mock, so every one of them
also checks the interface has more than one member.

## Consequences

- The interface is deliberately narrower than any vendor's API. Tools, JSON mode, logprobs
  — each is something one provider has and another does not, and a field for any of them is
  `None` on half the installations and load-bearing on the other half.
- Selection is `ZENITH_LLM_PROVIDER` against a dict keyed by `BaseLLMProvider.name`. Adding
  Anthropic or Bedrock is an adapter plus one line. An unknown name **fails with the valid
  list** rather than falling back — a typo that silently selected something would be a model
  swap nobody ordered, and `queries.model_used` would be the only place it showed.
- The class is `OpenAIProvider`; the module is `openai_compatible.py`. The protocol is
  OpenAI's, the servers speaking it are mostly not, and no *folder* is named after a vendor.
- `test_no_vendor_type_crosses_the_boundary` inspects `complete`'s signature on every
  registered provider, because the usual way this rots is a "temporary" passthrough of a
  vendor response for token counts.
