"""Which model answers, configured how, with which secret.

**Not to be confused with `adapters/` next door**, and the line between them is worth stating
because the names are close. `adapters/` holds the concrete ways to *speak* to a model — the
OpenAI-compatible HTTP shape, the mock. This package decides *which* of them a given request
uses: the tenant's stored row, the key it is encrypted with, and the provider built from both.
An adapter knows a protocol; a connector knows a customer.

The split exists because of ADR 0003: the LLM boundary is configured in the application rather
than the environment, so an on-premise customer changes model without a redeploy. That is what
gives this corner a database table and an encryption key at all — an installation that read its
model from `ZENITH_*` would need neither.
"""
