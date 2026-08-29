from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, created_at, uuid_pk

#: The lifecycle, in one place so a string literal never decides access anywhere.
ACTIVE = "active"
SUSPENDED = "suspended"
PURGING = "purging"
PURGED = "purged"


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid_pk]
    name: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[created_at]
    #: `active` | `suspended` | `purging` | `purged`.
    #:
    #: `suspended` cuts off access without touching a byte of data and is reversible.
    #: `purged` is the tombstone left behind after a purge: the rows are gone, the row here
    #: stays, so the name is not silently reused and the organisation's existence stays on
    #: record.
    status: Mapped[str] = mapped_column(default="active", server_default="active")
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The tenant's default label lives on `access_labels.is_default`, not here. A
    # `tenants.default_label_id` column would close a foreign-key cycle with
    # `access_labels.tenant_id`, which SQLAlchemy cannot order and which makes every
    # future schema operation on either table awkward.
