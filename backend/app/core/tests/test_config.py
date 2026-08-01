"""The guard on the value that signs every token.

Without it, an installation that forgets one environment variable lets anyone mint a
token for any user of any tenant — and RLS will enforce the forged context with complete
confidence. Every guarantee F1 and F2 built, defeated by an unset variable on someone
else's server.

So the guard is tested, for the same reason `verify_rls_active` is: a security control
nobody proved is a control that might not run.
"""

import pytest
from pydantic import ValidationError

from app.core.config import MINIMUM_SECRET_BYTES, Settings, generate_secret


def _settings(secret: str) -> Settings:
    return Settings(jwt_secret=secret)  # type: ignore[call-arg]  # the rest have defaults


def test_a_generated_secret_is_accepted() -> None:
    assert _settings(generate_secret()).jwt_secret


@pytest.mark.parametrize(
    "placeholder",
    ["dev-only-change-me", "change-me", "CHANGE-ME", "  changeme  ", "secret"],
)
def test_placeholders_are_refused(placeholder: str) -> None:
    """Including the one that used to be the default in this repository. Case and
    surrounding whitespace do not make it a different value."""
    with pytest.raises(ValidationError, match="placeholder"):
        _settings(placeholder)


def test_a_short_secret_is_refused() -> None:
    with pytest.raises(ValidationError, match=str(MINIMUM_SECRET_BYTES)):
        _settings("a" * (MINIMUM_SECRET_BYTES - 1))


def test_the_boundary_is_inclusive() -> None:
    """Exactly the minimum passes. A guard that is off by one rejects valid secrets and
    teaches operators to work around it."""
    assert _settings("a" * MINIMUM_SECRET_BYTES).jwt_secret


def test_length_is_counted_in_bytes_not_characters() -> None:
    """Twenty emoji are twenty characters and eighty bytes; twenty accented letters are
    twenty characters and forty. The security property is entropy in bytes, so that is
    what gets counted."""
    with pytest.raises(ValidationError):
        _settings("é" * 15)  # 15 characters, 30 bytes

    assert _settings("é" * 16).jwt_secret  # 16 characters, 32 bytes


def test_an_empty_secret_is_refused_with_our_own_message() -> None:
    """Not Pydantic's "Field required".

    The person reading this is installing the product. `jwt_secret` carries an empty
    default purely so the failure is a sentence they can act on.
    """
    with pytest.raises(ValidationError, match="is not set"):
        _settings("")


def test_the_error_says_how_to_fix_it_without_needing_the_CLI() -> None:
    """The advice must not be "run `zenith generate-secret`".

    `Settings` is built when `app.core.config` is imported and every CLI command imports
    it, so that command cannot start without the value it exists to produce. Telling an
    operator to run something that cannot run is worse than saying nothing.
    """
    with pytest.raises(ValidationError, match="import secrets"):
        _settings("too-short")

    with pytest.raises(ValidationError, match="import secrets"):
        _settings("")


def test_generated_secrets_are_not_predictable() -> None:
    assert generate_secret() != generate_secret()
    assert len(generate_secret().encode()) >= MINIMUM_SECRET_BYTES
