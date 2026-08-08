from sqlalchemy import ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, created_at, uuid_col, uuid_pk


class AccessLabel(Base):
    """Access label (RF-04.2).

    A document carries one or more; a role reaches a set of them. The intersection
    decides visibility, and RLS enforces it.
    """

    __tablename__ = "access_labels"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name"),
        # At most one default per tenant, enforced by the database. The alternative —
        # application code clearing the old default before setting a new one — is correct
        # until the first path that forgets, and then a tenant has two defaults and
        # uploads land wherever the query planner felt like ordering them.
        Index(
            "uq_access_labels_one_default_per_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
        # `GET /labels/search`'s recency ordering. `id` is in the key because `created_at`
        # is not unique: migration 0007 backfilled every pre-existing row with the same
        # timestamp, so a cursor on the timestamp alone could not separate them.
        Index(
            "ix_access_labels_recency",
            "tenant_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid_col] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    name: Mapped[str]
    created_at: Mapped[created_at]
    # Applied when an upload names no label (mvp.md §2.2). Lives here rather than as
    # `tenants.default_label_id` because that column would close a foreign-key cycle
    # between the two tables.
    is_default: Mapped[bool] = mapped_column(default=False, server_default="false")
    #: How much clearance this label demands, 0 to 10.
    #:
    #: **0 means compartment**, and it is the default: no clearance reaches this label, only
    #: an explicit `role_labels` grant. That is the model the product shipped with, and it
    #: is what a label should be unless somebody decides otherwise — a compartment is not
    #: "low security", it is "orthogonal to seniority", and HR salary data is the example
    #: that makes the distinction matter.
    #:
    #: 1 to 10 makes it a classification: any role whose `priority_level` is at or above it
    #: reaches it without a grant.
    priority_level: Mapped[int] = mapped_column(default=0, server_default="0")


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
