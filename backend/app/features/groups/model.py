"""Functional groups: the horizontal half of access.

A group is a part of the business — `Finance`, `Human Resources`, `Engineering`,
`Project-Alpha`. It answers "whose material is this?", which is a different question from
"how sensitive is it?", and the product needed both because an administrator was previously
expressing them with one mechanism.

Groups do not nest. A hierarchy of groups would need a rule for whether a parent's members
inherit a child's access, and every answer to that is somebody's security incident: yes
makes `Engineering` read `Engineering/Payroll`, no makes the tree decorative. Clearance
already provides the vertical axis, so the horizontal one stays flat.
"""

from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, created_at, uuid_col, uuid_pk


class Group(Base):
    __tablename__ = "groups"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_groups_tenant_name"),)

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid_col] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    name: Mapped[str]
    description: Mapped[str | None]
    created_at: Mapped[created_at]


class UserGroup(Base):
    __tablename__ = "user_groups"
    __table_args__ = (Index("ix_user_groups_user", "user_id"),)

    user_id: Mapped[uuid_col] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[uuid_col] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )


class GroupLabel(Base):
    """Which labels a group's members may reach — subject to clearance.

    A label mapped to no group at all is unreachable by this route, which is what keeps
    every label that existed before groups did behaving exactly as it did: reachable only
    through an explicit `role_labels` grant.
    """

    __tablename__ = "group_labels"
    __table_args__ = (Index("ix_group_labels_label", "label_id"),)

    group_id: Mapped[uuid_col] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    label_id: Mapped[uuid_col] = mapped_column(
        ForeignKey("access_labels.id", ondelete="CASCADE"), primary_key=True
    )
