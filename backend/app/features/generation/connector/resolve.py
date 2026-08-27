"""Which model this tenant's requests go to.

The answer was written twice — once in `AnswerService._resolve`, once in
`Classifier._resolve` — identically, down to the `LIMIT 1`. The classifier's copy even said so
in its docstring: *"The same resolution `AnswerService._resolve` performs."* Two copies of an
access-shaped decision is one copy too many: the day a tenant's configuration grows a field,
or the fallback changes, one of them gets it and the other does not, and the symptom is a
classifier quietly talking to a different model than the answers do.

Read inside the caller's own `tenant_session`, which is the property worth stating. The policy
`tenant_id = zenith_current_tenant()` does the scoping, so this needs no bypass and the
surface ADR 0001 audits stays at four routes. There is nothing here a customer's own session
may not read — it is their configuration.
"""

from sqlalchemy import text

from app.common.llm import BaseLLMProvider
from app.core.database import tenant_session
from app.features.generation.connector import providers
from app.features.generation.connector.crypto import decrypt
from app.features.tenancy.context import TenantContext


async def provider_for(context: TenantContext) -> BaseLLMProvider:
    """The tenant's own configuration, or the installation's, built by the registry.

    This reads a row and returns a `Configuration`; `providers.build` decides what class that
    becomes. The split is what keeps provider selection in one place — otherwise "which
    adapter runs" would be answered here for tenants and in settings for everyone else, and
    the two would drift.
    """
    async with tenant_session(context) as session:
        row = (
            await session.execute(
                text("SELECT endpoint_url, model_name, api_key_encrypted FROM llm_config LIMIT 1")
            )
        ).first()

    if row is None:
        return providers.build(providers.from_settings())

    return providers.build(
        providers.Configuration(
            # A tenant configures an endpoint and a model, never an adapter: which adapter
            # speaks to that endpoint is an operator's decision about the installation, not a
            # customer's about their account.
            provider=providers.from_settings().provider,
            endpoint_url=row.endpoint_url,
            model=row.model_name,
            api_key=decrypt(row.api_key_encrypted) if row.api_key_encrypted else None,
        )
    )
