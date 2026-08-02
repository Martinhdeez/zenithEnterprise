import secrets
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Values that must never reach an installation. Kept as data rather than as a single
# string comparison because the list only grows: every placeholder that ever appears in
# a README or a docker-compose example belongs here.
UNSAFE_SECRETS = frozenset(
    {
        "dev-only-change-me",
        "change-me",
        "changeme",
        "secret",
    }
)

MINIMUM_SECRET_BYTES = 32

# Deliberately not "run `zenith generate-secret`", even though that command exists.
# `Settings` is constructed when `app.core.config` is imported, and every CLI command
# imports it — so the command whose entire purpose is to produce the missing secret cannot
# start without it. An operator installing the product hits that immediately, and being
# told to run something that cannot run is worse than no advice at all.
#
# This one-liner needs nothing but a Python interpreter, which they already have.
HOW_TO_GENERATE = (
    '\n  Generate one with:  python -c "import secrets; print(secrets.token_urlsafe(48))"'
)


def generate_secret() -> str:
    """A secret nobody has to think about.

    The validator below catches carelessness, not bad judgement: 32 identical characters
    pass every check it can make. The real answer is that the operator never chooses the
    value at all, which is what `zenith generate-secret` is for.
    """
    return secrets.token_urlsafe(48)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ZENITH_", env_file=".env", extra="ignore")

    # Application role, subject to RLS. This is what the API uses.
    database_url: str = "postgresql+psycopg://zenith_app:change-me@localhost:5432/zenith"
    # Schema owner, bypasses RLS. Migrations and the install CLI only.
    database_owner_url: str = "postgresql+psycopg://zenith:zenith@localhost:5432/zenith"
    tei_embed_url: str = "http://localhost:8081"
    tei_rerank_url: str = "http://localhost:8082"

    # Empty is not a usable default — the validator rejects it. It exists only so the
    # failure is *our* message rather than Pydantic's "Field required", because the person
    # reading it is installing the product and needs to be told what to do about it.
    jwt_secret: str = ""
    access_token_minutes: int = 15
    refresh_token_days: int = 14
    # Fernet key protecting customer API keys at rest. Still optional because nothing
    # reads it yet; it gets the same guard as `jwt_secret` when the generation connector
    # is built, and refusing to start over a feature that does not exist would be theatre.
    encryption_key: str = ""

    # Where uploaded documents are stored, content-addressed. Absolute on purpose: a
    # relative path would resolve against the working directory, so a service restarted
    # from somewhere else would find an empty corpus and report no data loss at all.
    storage_dir: Path = Path("/var/lib/zenith/documents")

    # Limits from mvp.md 2.12
    max_file_bytes: int = 100 * 1024 * 1024
    max_documents_per_tenant: int = 5_000
    max_pages_per_document: int = 3_000
    max_queries_per_minute_user: int = 30
    max_queries_per_minute_tenant: int = 120
    # Login is the expensive unauthenticated endpoint: argon2 costs real CPU and RAM
    # per attempt, by design. See `features/auth/throttle.py`.
    login_attempts_per_minute: int = 10
    password_hash_concurrency: int = 4

    # Separate pools: the query path must not compete with ingestion.
    api_pool_size: int = 10
    worker_pool_size: int = 5
    statement_timeout_ms: int = 10_000

    @field_validator("jwt_secret")
    @classmethod
    def secret_must_be_real(cls, value: str) -> str:
        """Refuse to start rather than warn.

        This signs every access token. With a known or guessable value, anyone can mint
        a token for any user of any tenant, and RLS will enforce the forged context with
        complete confidence — every guarantee F1 and F2 built, defeated by one unset
        environment variable on someone else's server.

        A silent security failure is worse than none, which is the same reason
        `verify_rls_active` refuses to serve. Raising here means Pydantic fails during
        settings construction, the process exits non-zero, and the port never opens.
        """
        if not value:
            raise ValueError(f"ZENITH_JWT_SECRET is not set.{HOW_TO_GENERATE}")
        if value.strip().lower() in UNSAFE_SECRETS:
            raise ValueError(
                f"ZENITH_JWT_SECRET is set to the placeholder {value!r}.{HOW_TO_GENERATE}"
            )
        if len(value.encode()) < MINIMUM_SECRET_BYTES:
            raise ValueError(
                f"ZENITH_JWT_SECRET must be at least {MINIMUM_SECRET_BYTES} bytes "
                f"(got {len(value.encode())}).{HOW_TO_GENERATE}"
            )
        return value


settings = Settings()  # type: ignore[call-arg]  # values come from the environment
