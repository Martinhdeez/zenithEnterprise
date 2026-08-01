from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, uuid_col


class LlmConfig(Base):
    """Pluggable connector, per tenant (mvp.md 2.11).

    The key is encrypted at rest. `certification_result` stores the outcome of the
    RNF-06 suite: no model counts as supported until it passes.
    """

    __tablename__ = "llm_config"

    tenant_id: Mapped[uuid_col] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    endpoint_url: Mapped[str]
    api_key_encrypted: Mapped[str | None]
    model_name: Mapped[str]
    certified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    certification_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
