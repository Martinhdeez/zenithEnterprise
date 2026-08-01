from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, created_at, uuid_pk


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid_pk]
    name: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[created_at]
    # The tenant's default label lives on `access_labels.is_default`, not here. A
    # `tenants.default_label_id` column would close a foreign-key cycle with
    # `access_labels.tenant_id`, which SQLAlchemy cannot order and which makes every
    # future schema operation on either table awkward.
