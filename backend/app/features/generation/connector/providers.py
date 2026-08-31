"""Which adapter runs, decided by configuration rather than by an import.

The registry is a dict, not a plugin system. Adding a provider means writing an adapter and
adding one line here — and that line is the point: the set of providers an installation can
be pointed at is a fact the codebase states, not something discovered at runtime from an
environment variable. An unknown `ZENITH_LLM_PROVIDER` fails with the list of valid names
rather than falling back to a default the operator did not choose.

`build` never returns a half-configured provider. Everything it needs comes from either the
tenant's `llm_config` row or settings, and a missing endpoint is an error here rather than
a connection refused at the first question.
"""

from dataclasses import dataclass

from app.common.llm import BaseLLMProvider, GenerationUnavailableError
from app.core.config import settings
from app.features.generation.adapters.gemini_native import GeminiProvider
from app.features.generation.adapters.mock import MockProvider
from app.features.generation.adapters.openai_compatible import OpenAIProvider

# Keyed by `BaseLLMProvider.name`. Anthropic, Bedrock and a local llama.cpp binding join
# this dict and change nothing else — that is the whole return on the abstraction.
PROVIDERS = (OpenAIProvider, GeminiProvider, MockProvider)
BY_NAME = {provider.name: provider for provider in PROVIDERS}


@dataclass(frozen=True, slots=True)
class Configuration:
    """Where a provider's settings came from, flattened.

    A dataclass rather than passing the database row around, so that `build` cannot tell a
    tenant's configuration from the installation default and therefore cannot treat them
    differently by accident.
    """

    provider: str
    endpoint_url: str
    model: str
    api_key: str | None = None


def from_settings() -> Configuration:
    return Configuration(
        provider=settings.llm_provider,
        endpoint_url=settings.llm_endpoint_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key or None,
    )


def build(configuration: Configuration) -> BaseLLMProvider:
    provider = BY_NAME.get(configuration.provider)
    if provider is None:
        raise GenerationUnavailableError(
            f"unknown LLM provider {configuration.provider!r}. "
            f"ZENITH_LLM_PROVIDER must be one of: {', '.join(sorted(BY_NAME))}."
        )

    if provider is MockProvider:
        # Deliberately configurable to nothing: it has no endpoint and no key, and
        # demanding either would make the diagnostic path need the thing it is diagnosing.
        return MockProvider()

    if not configuration.endpoint_url or not configuration.model:
        raise GenerationUnavailableError(
            "no language model is configured for this tenant, and the installation default "
            "is incomplete. Someone holding llm_config.manage has to configure one."
        )

    # Written out rather than `provider(**fields)`. The two HTTP adapters happen to take the
    # same three arguments today, and constructing whichever one the registry returned would
    # read as though that were guaranteed — it is not, and the first adapter that needs a
    # fourth would fail at a call site instead of here. `MockProvider` above is already the
    # proof that the signatures are not uniform.
    if provider is GeminiProvider:
        return GeminiProvider(
            endpoint_url=configuration.endpoint_url,
            model=configuration.model,
            api_key=configuration.api_key,
        )
    return OpenAIProvider(
        endpoint_url=configuration.endpoint_url,
        model=configuration.model,
        api_key=configuration.api_key,
    )
