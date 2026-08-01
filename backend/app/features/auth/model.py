from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, created_at, uuid_col, uuid_pk


class Permission(Base):
    """Closed catalogue, defined by the software (mvp.md 2.1).

    No `tenant_id`: the administrator composes roles out of this list, they do not
    extend it. An invented permission would have nothing enforcing it.
    """

    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(primary_key=True)
    description: Mapped[str]


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email"),
        # The unique constraint above cannot serve login, which knows the email but
        # not yet the tenant. Declared here as well as in migration 0002 so the drift
        # test keeps them in step.
        Index("ix_users_email", "email"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid_col] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    # Stored lowercased; see `normalise_email`. Uniqueness is per tenant, not per
    # installation, so the same person can exist in two tenants of a hosted install.
    email: Mapped[str]
    password_hash: Mapped[str]
    # Immediate revocation without Redis: bumping this invalidates live tokens.
    token_version: Mapped[int] = mapped_column(default=0, server_default="0")
    created_at: Mapped[created_at]


class Role(Base):
    """Created by the tenant administrator, under whatever name they choose."""

    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid_col] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    name: Mapped[str]
    # System roles cannot be deleted: stops an administrator from removing the
    # last role holding administration permissions.
    is_system: Mapped[bool] = mapped_column(default=False, server_default="false")


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[uuid_col] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_code: Mapped[str] = mapped_column(
        ForeignKey("permissions.code", ondelete="CASCADE"), primary_key=True
    )


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[uuid_col] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[uuid_col] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
