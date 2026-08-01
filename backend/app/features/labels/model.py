from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, uuid_col, uuid_pk


class AccessLabel(Base):
    """Access label (RF-04.2).

    A document carries one or more; a role reaches a set of them. The intersection
    decides visibility, and RLS enforces it.
    """

    __tablename__ = "access_labels"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid_col] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    name: Mapped[str]


class RoleLabel(Base):
    __tablename__ = "role_labels"

    role_id: Mapped[uuid_col] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    label_id: Mapped[uuid_col] = mapped_column(
        ForeignKey("access_labels.id", ondelete="CASCADE"), primary_key=True
    )


class DocumentLabel(Base):
    __tablename__ = "document_labels"

    document_id: Mapped[uuid_col] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    label_id: Mapped[uuid_col] = mapped_column(
        ForeignKey("access_labels.id", ondelete="CASCADE"), primary_key=True
    )
