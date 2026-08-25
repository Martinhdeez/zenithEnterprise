"""Configuring the generation connector (mvp.md 2.11).

The admin screen behind this stores an endpoint, a model name and a key. Two rules make it
different from ordinary CRUD.

**The key is encrypted at rest and never read back.** `GET` returns whether a key is stored,
never the key — an administration screen that displays a credential turns every support
screenshot and every shoulder into a disclosure, and nobody ever needs to *read* it, only
to replace it.

**Nothing here validates that the endpoint works.** §2.11 wants a "test connection" button
that runs the RNF-06 certification suite, and that is a real feature with real substance;
pretending a 200 from `/v1/models` is certification would be worse than admitting there is
none yet. Configuration saves what it was given, and the first query reports honestly if it
is wrong.
"""

from dataclasses import dataclass

from sqlalchemy import text

from app.core.database import tenant_session
from app.features.auth.service import AccessProfile
from app.features.generation.crypto import encrypt

MANAGE = "llm_config.manage"


@dataclass(frozen=True, slots=True)
class LlmSettings:
    endpoint_url: str
    model_name: str
    #: Whether a key is stored — never the key. See the module docstring.
    has_api_key: bool
    configured: bool


class LlmConfigService:
    def __init__(self, profile: AccessProfile) -> None:
        self.context = profile.context

    async def get(self) -> LlmSettings:
        """This tenant's configuration, or the installation default it falls back to.

        Reporting the fallback rather than emptiness, because "not configured" and "using
        the installation's model" look identical to an administrator otherwise, and only
        one of them is a problem.
        """
        from app.core.config import settings

        async with tenant_session(self.context) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT endpoint_url, model_name, api_key_encrypted IS NOT NULL "
                        "AS has_key FROM llm_config LIMIT 1"
                    )
                )
            ).first()

        if row is None:
            return LlmSettings(
                endpoint_url=settings.llm_endpoint_url,
                model_name=settings.llm_model,
                has_api_key=bool(settings.llm_api_key),
                configured=False,
            )
        return LlmSettings(
            endpoint_url=row.endpoint_url,
            model_name=row.model_name,
            has_api_key=bool(row.has_key),
            configured=True,
        )

    async def put(
        self, endpoint_url: str, model_name: str, api_key: str | None = None
    ) -> LlmSettings:
        """Store or replace this tenant's connector.

        `api_key=None` leaves an existing key untouched rather than clearing it. An
        administrator editing the model name should not have to re-enter a credential they
        cannot read, and silently wiping it would break generation on a save that looked
        harmless. Clearing is `api_key=""`, which is explicit.
        """
        encrypted = encrypt(api_key) if api_key else None

        async with tenant_session(self.context) as session:
            await session.execute(
                text(
                    "INSERT INTO llm_config (tenant_id, endpoint_url, model_name, "
                    "api_key_encrypted) VALUES (:t, :url, :model, :key) "
                    "ON CONFLICT (tenant_id) DO UPDATE SET "
                    "  endpoint_url = EXCLUDED.endpoint_url, "
                    "  model_name = EXCLUDED.model_name, "
                    # COALESCE keeps the stored key when none was supplied; an empty
                    # string arrives as NULL from the caller and clears it deliberately.
                    "  api_key_encrypted = COALESCE("
                    "     EXCLUDED.api_key_encrypted, llm_config.api_key_encrypted), "
                    "  certified_at = NULL, certification_result = NULL"
                ),
                {
                    "t": self.context.tenant_id,
                    "url": endpoint_url,
                    "model": model_name,
                    "key": encrypted,
                },
            )
        # Certification is cleared on every change, and that is the point of storing it:
        # a model certified last month says nothing about the endpoint saved a moment ago.
        return await self.get()

    async def clear(self) -> None:
        """Fall back to the installation default.

        Deleting the row rather than blanking it, so `get` reports `configured: false` and
        an administrator can tell "we removed ours" from "somebody saved an empty one".
        """
        async with tenant_session(self.context) as session:
            await session.execute(text("DELETE FROM llm_config"))
