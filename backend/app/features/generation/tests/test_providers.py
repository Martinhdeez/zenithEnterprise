"""The boundary, asserted rather than described.

A layering rule that lives only in a docstring is a rule that holds until the first person
in a hurry. These are the parts of it a test can actually check.
"""

import inspect

import pytest

from app.common.llm import BaseLLMProvider, GenerationResponse, GenerationUnavailableError
from app.core.config import settings
from app.features.generation.adapters.mock import MockProvider
from app.features.generation.adapters.openai_compatible import OpenAIProvider
from app.features.generation.connector import providers


def test_every_registered_provider_implements_the_interface() -> None:
    """An ABC rather than a Protocol, so this fails at construction rather than at a call
    site somewhere in the application layer."""
    for provider in providers.PROVIDERS:
        assert issubclass(provider, BaseLLMProvider)
        assert provider.name, f"{provider.__name__} must name itself for ZENITH_LLM_PROVIDER"


def test_provider_names_are_unique() -> None:
    """The registry is a dict, so a duplicate would silently shadow rather than fail."""
    assert len(providers.BY_NAME) == len(providers.PROVIDERS)


def test_the_provider_is_chosen_by_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_provider", "mock")

    assert isinstance(providers.build(providers.from_settings()), MockProvider)

    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_endpoint_url", "http://somewhere/v1")
    monkeypatch.setattr(settings, "llm_model", "a-model")

    assert isinstance(providers.build(providers.from_settings()), OpenAIProvider)


def test_an_unknown_provider_lists_the_valid_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rather than falling back to a default the operator did not choose. A typo in
    `ZENITH_LLM_PROVIDER` that silently selected something would be a model swap nobody
    ordered — and `queries.model_used` would be the only place it showed."""
    monkeypatch.setattr(settings, "llm_provider", "anthropic-typo")

    with pytest.raises(GenerationUnavailableError, match="mock, openai"):
        providers.build(providers.from_settings())


def test_an_incomplete_configuration_fails_before_the_first_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_endpoint_url", "")

    with pytest.raises(GenerationUnavailableError, match="llm_config.manage"):
        providers.build(providers.from_settings())


def test_no_vendor_type_crosses_the_boundary() -> None:
    """The rule `common/llm.py` exists to hold, checked at the one place it can be.

    `complete` is the only way out of an adapter, so its annotations are the whole surface.
    If a vendor response type ever becomes the return type — the usual way this rots, via a
    "temporary" passthrough for token counts — this fails.
    """
    for provider in providers.PROVIDERS:
        signature = inspect.signature(provider.complete)
        assert signature.return_annotation is GenerationResponse
        assert [parameter for parameter in signature.parameters] == ["self", "system", "user"]


async def test_the_mock_provider_answers_without_a_network() -> None:
    """Its second job: an installation can answer before an LLM is configured, which is what
    separates "is retrieval working?" from "is the model reachable?" when both are new."""
    mock = MockProvider(["first [1]", "second [1]"])

    assert (await mock.complete("s", "u")).text == "first [1]"
    assert (await mock.complete("s", "u")).text == "second [1]"
    # Repeats the last entry rather than raising: a test asserting on one answer should not
    # also have to know how many times the service calls the model.
    assert (await mock.complete("s", "u")).text == "second [1]"
    assert len(mock.calls) == 3
