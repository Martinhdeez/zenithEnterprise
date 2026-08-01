from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, created_at, uuid_pk


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid_pk]
    name: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[created_at]
