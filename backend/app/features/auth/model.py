from sqlalchemy import ForeignKey, Index, Text, UniqueConstraint
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
        # Installation-wide since 0011, and not a tightening for tidiness:
        # `AuthService.authenticate` refuses any address that matches more than one row, so
        # a duplicate did not create two usable accounts — it created two unusable ones,
        # visible only as `login_ambiguous_email` in the server log.
        Index("uq_users_email_global", "email", unique=True),
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
    # Optional and staying that way: an invitation that demanded a name would put a
    # required field in front of an administrator who may only know the address. Every
    # place that renders it falls back to the email.
    name: Mapped[str | None] = mapped_column(Text)
    password_hash: Mapped[str]
    # Immediate revocation without Redis: bumping this invalidates live tokens.
    token_version: Mapped[int] = mapped_column(default=0, server_default="0")
    created_at: Mapped[created_at]
    #: Authority above every tenant, and the one field on this table the application
    #: connection cannot write.
    #:
    #: It is not a permission in `CATALOGUE` on purpose: a tenant's administrator edits
    #: `role_permissions` freely from the roles screen, so a permission would be a route out
    #: of their own tenant. Migration 0010 narrows `zenith_app`'s UPDATE grant to a column
    #: list that omits this one, so promoting yourself fails at the database whatever code
    #: path tries it. Granted from the CLI only.
    is_system_admin: Mapped[bool] = mapped_column(default=False, server_default="false")


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
    #: How much clearance the holder has, 1 (lowest) to 10. Reaches every label whose own
    #: level is at or below it — and no label at all by that route until an administrator
    #: gives one a level, since labels start at 0. See `AccessLabel.priority_level`.
    priority_level: Mapped[int] = mapped_column(default=1, server_default="1")


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
