"""The compose file and the hardware profile table must not be able to disagree.

F11 measured what happens when they do, on the VPS, with the whole stack running:
`tei-rerank` was started without batch flags, so it kept TEI's small defaults while
`TeiReranker.plan_batches` built batches sized by the profile. Every rerank request came
back rejected —

    batch size 14 > maximum allowed batch size 4

— and `SearchService` did exactly what it was designed to do: caught it, fell back to the
fused order, and returned a good answer marked `degraded`. Nothing crashed, no test failed,
and the only symptom was recall fifteen points below what F7 measured.

That is the most expensive kind of bug this project can ship: a paid-for component that
never runs, on someone else's server, with no error anywhere. These tests are cheap and
they close it.
"""

import re
from pathlib import Path

import pytest

from app.core.hardware import PROFILES

COMPOSE = Path(__file__).resolve().parents[3] / "docker" / "docker-compose.yml"

# The compose defaults describe the smallest supported machine, because an operator who
# does not set the variables gets whatever the file says and the smallest profile is the
# only safe thing to assume about a machine you have not seen.
DEFAULT_PROFILE = "low-spec"


def flags(service: str) -> dict[str, str]:
    """The TEI flags a service is started with, read from the file rather than from a guess.

    Parsed with a regex over the service's block instead of with a YAML library. `pyyaml`
    is not a dependency of this project, and adding one so a test can read six strings would
    be a dependency to audit and license for the next decade of an on-premise product.
    """
    body = COMPOSE.read_text()
    start = body.index(f"  {service}:")
    following = [
        body.index(f"  {name}:")
        for name in ("db", "tei-embed", "tei-rerank", "api", "worker")
        if body.index(f"  {name}:") > start
    ]
    block = body[start : min(following) if following else len(body)]
    return {
        name: value
        for name, value in re.findall(r'"--(max-[a-z-]+)"\s*\n\s*-\s*"\$\{(\w+):-\d+\}"', block)
    }


def defaults(service: str) -> dict[str, int]:
    body = COMPOSE.read_text()
    start = body.index(f"  {service}:")
    following = [
        body.index(f"  {name}:")
        for name in ("db", "tei-embed", "tei-rerank", "api", "worker")
        if body.index(f"  {name}:") > start
    ]
    block = body[start : min(following) if following else len(body)]
    return {
        name: int(fallback)
        for name, fallback in re.findall(r'"--(max-[a-z-]+)"\s*\n\s*-\s*"\$\{\w+:-(\d+)\}"', block)
    }


@pytest.mark.parametrize("service", ["tei-embed", "tei-rerank"])
def test_every_tei_service_declares_its_batch_limits(service: str) -> None:
    """Absence is worse than a wrong value.

    A wrong number fails loudly on the first request. A missing flag silently keeps TEI's
    default, which is smaller than every profile in the table, so the component degrades
    forever and reports success.
    """
    declared = defaults(service)

    assert "max-batch-tokens" in declared, f"{service} must pin --max-batch-tokens"
    assert "max-client-batch-size" in declared, f"{service} must pin --max-client-batch-size"


def test_the_two_tei_services_read_the_same_variables() -> None:
    """One setting must never be configurable in two places that can disagree.

    If the reranker took `TEI_RERANK_MAX_BATCH_TOKENS` and the embedder took
    `TEI_MAX_BATCH_TOKENS`, an operator would set one, believe they had configured TEI, and
    leave the other on a default that contradicts the profile.
    """
    assert flags("tei-embed") == flags("tei-rerank")


@pytest.mark.parametrize("service", ["tei-embed", "tei-rerank"])
def test_the_compose_defaults_match_the_smallest_profile(service: str) -> None:
    """An operator who sets nothing gets the smallest supported machine's settings.

    `low-spec` rather than `cpu`, because the only safe assumption about a machine nobody
    has measured is that it is the smallest one supported. Guessing upwards is how M0's TEI
    reached 6.59 GB on a 7.6 GB box and was killed before it read a single document.
    """
    profile = PROFILES[DEFAULT_PROFILE]
    declared = defaults(service)

    assert declared["max-batch-tokens"] == profile.max_batch_tokens
    assert declared["max-client-batch-size"] == profile.max_client_batch_size
