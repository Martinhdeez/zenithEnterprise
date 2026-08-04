"""The provider API key at rest, and the two ways an operator gets it wrong."""

import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings, settings
from app.features.generation.crypto import EncryptionUnavailableError, decrypt, encrypt


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> str:
    generated = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "encryption_key", generated)
    return generated


def test_a_key_survives_the_round_trip(key: str) -> None:
    assert decrypt(encrypt("sk-customer-secret")) == "sk-customer-secret"


def test_the_ciphertext_does_not_contain_the_key(key: str) -> None:
    """The point of the exercise: a database dump leaves the building — for a backup, for
    a support request — and a key in plaintext there is a key in plaintext in an inbox."""
    assert "sk-customer-secret" not in encrypt("sk-customer-secret")


def test_storing_a_key_without_an_encryption_key_configured_says_how_to_fix_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raised here rather than at startup, deliberately: an installation on the local model
    stores no provider key at all, and refusing to boot it over a credential it does not
    have would be theatre. The failure lands on the person who can act on it."""
    monkeypatch.setattr(settings, "encryption_key", "")

    with pytest.raises(EncryptionUnavailableError, match="Fernet.generate_key"):
        encrypt("sk-customer-secret")


def test_a_rotated_encryption_key_is_named_as_the_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    """The realistic incident: the key was rotated, or the database was restored from a
    backup taken under a different one. "Invalid token" sends someone to look at the
    database, which is the one place the problem is not."""
    monkeypatch.setattr(settings, "encryption_key", Fernet.generate_key().decode())
    stored = encrypt("sk-customer-secret")
    monkeypatch.setattr(settings, "encryption_key", Fernet.generate_key().decode())

    with pytest.raises(EncryptionUnavailableError, match="different one"):
        decrypt(stored)


def test_a_malformed_encryption_key_is_refused_at_startup() -> None:
    """Knowable at boot, so caught at boot — otherwise the first failure is months later,
    when a tenant configures a connector on a machine nobody is watching."""
    with pytest.raises(ValueError, match="valid Fernet key"):
        Settings(jwt_secret="x" * 40, encryption_key="not-a-real-key")  # type: ignore[call-arg]
