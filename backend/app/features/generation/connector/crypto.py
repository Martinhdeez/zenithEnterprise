"""The customer's API key, at rest.

`llm_config.api_key_encrypted` holds a credential to a system this product does not own —
often a corporate account with a budget attached. A database dump is a plausible event on
an on-premise install (backups leave the building, support asks for one), and a key in
plaintext there is a key in plaintext in whoever's inbox.

Fernet from `cryptography`: AES-128-CBC with an HMAC, authenticated, with the key handling
already made unforgeable by the library. The alternative — assembling this from primitives
— is how a project ends up with an unauthenticated cipher and a reused IV.
"""

from cryptography.fernet import Fernet, InvalidToken

from app.common.exceptions import ZenithError
from app.core.config import settings

HOW_TO_GENERATE = (
    "\n  Generate one with:  "
    'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
)


class EncryptionUnavailableError(ZenithError):
    """No usable `ZENITH_ENCRYPTION_KEY`, and something needed one.

    Raised here rather than at startup, deliberately. `config.py` promised this value the
    same guard as `jwt_secret` "when the generation connector is built" — but the guard that
    fits is not the same one. `jwt_secret` signs every token on every installation; this key
    protects one thing, a remote provider's credential, and an installation running the
    local Ollama baseline stores none. Refusing to boot such an installation over a
    credential it does not have would be theatre in a different costume.

    So the failure lands on the person storing a key, who is the only person who can fix it.
    """

    status_code = 500
    code = "encryption_unavailable"


def _cipher() -> Fernet:
    if not settings.encryption_key:
        raise EncryptionUnavailableError(
            f"ZENITH_ENCRYPTION_KEY is not set, and storing a provider API key requires "
            f"it.{HOW_TO_GENERATE}"
        )
    return Fernet(settings.encryption_key.encode())


def encrypt(secret: str) -> str:
    return _cipher().encrypt(secret.encode()).decode()


def decrypt(token: str) -> str:
    """A key that will not decrypt is a configuration error, not a corrupt row.

    The realistic cause is `ZENITH_ENCRYPTION_KEY` having been rotated or restored from a
    different backup than the database. Saying so is what turns a mystery into a five-minute
    fix; "invalid token" sends someone looking at the database.
    """
    try:
        return _cipher().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise EncryptionUnavailableError(
            "the stored provider API key cannot be decrypted with the current "
            "ZENITH_ENCRYPTION_KEY. It was encrypted with a different one — restore that "
            "key, or reconfigure the connector with a fresh API key."
        ) from exc
