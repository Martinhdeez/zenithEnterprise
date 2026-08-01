from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ZENITH_", env_file=".env", extra="ignore")

    # Application role, subject to RLS. This is what the API uses.
    database_url: str = "postgresql+psycopg://zenith_app:change-me@localhost:5432/zenith"
    # Schema owner, bypasses RLS. Migrations and the install CLI only.
    database_owner_url: str = "postgresql+psycopg://zenith:zenith@localhost:5432/zenith"
    tei_embed_url: str = "http://localhost:8081"
    tei_rerank_url: str = "http://localhost:8082"

    jwt_secret: str = "dev-only-change-me"
    access_token_minutes: int = 15
    refresh_token_days: int = 14
    # Fernet key protecting customer API keys at rest.
    encryption_key: str = ""

    # Limits from mvp.md 2.12
    max_file_bytes: int = 100 * 1024 * 1024
    max_documents_per_tenant: int = 5_000
    max_pages_per_document: int = 3_000
    max_queries_per_minute_user: int = 30
    max_queries_per_minute_tenant: int = 120

    # Separate pools: the query path must not compete with ingestion.
    api_pool_size: int = 10
    worker_pool_size: int = 5
    statement_timeout_ms: int = 10_000


settings = Settings()
