"""functional groups and clearance levels

Revision ID: 0009
Revises: 0008

Access has been one dimension since 0001: a document is visible if its labels intersect the
labels the caller's roles were granted. That is a compartment model, and it works, but it
makes an administrator express two different ideas with one mechanism.

The two ideas are horizontal and vertical.

**Horizontal** is which part of the business you are in. Engineering does not read Human
Resources, not because HR is more secret but because it is somebody else's. That is what
`groups` is: departments, functions, project teams.

**Vertical** is how sensitive the material is. Within Finance, a Viewer reads the monthly
report and does not read the confidential one. That is `priority_level`, on roles as how
much clearance the holder has and on labels as how much the label demands.

A label reachable through a group requires *both*: the caller is in one of the label's
groups **and** carries the clearance the label asks for. Group membership is not seniority
and seniority is not membership, so neither alone opens anything.

**Nothing widens when this migration runs.** Labels start at `priority_level = 0` and with
no rows in `group_labels`, and a label mapped to no group is reachable only by the explicit
`role_labels` grant that already reached it. Every tenant wakes up with exactly the access
it went to sleep with, and a label becomes group- or clearance-governed when an
administrator says so, one label at a time.

The alternative — defaulting labels into a group or to level 1 so the feature "works" out
of the box — would rearrange who can read what across every existing tenant during a schema
upgrade, silently, with no administrator having chosen any of it.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Tenant-scoped like `roles` and `access_labels`: the row carries its own `tenant_id`.
TENANT_POLICIES = {"groups": "tenant_id = zenith_current_tenant()"}

# Derived, so the parent's policy is applied by the EXISTS rather than restated. A group in
# another tenant is not forbidden here — it is absent, which is the same reason
# `user_roles` is written this way.
DERIVED_POLICIES = {
    "user_groups": "EXISTS (SELECT 1 FROM users u WHERE u.id = user_groups.user_id)",
    "group_labels": "EXISTS (SELECT 1 FROM groups g WHERE g.id = group_labels.group_id)",
}


def upgrade() -> None:
    op.add_column(
        "roles",
        sa.Column("priority_level", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "access_labels",
        sa.Column("priority_level", sa.Integer(), nullable=False, server_default="0"),
    )
    # Checked in the database, not only in pydantic. The range is a security statement — a
    # role at level 11 would out-rank every label — and the API is not the only writer:
    # the install CLI writes these tables, and so does anyone with psql.
    op.create_check_constraint(
        "ck_roles_priority_level", "roles", "priority_level BETWEEN 1 AND 10"
    )
    op.create_check_constraint(
        "ck_access_labels_priority_level", "access_labels", "priority_level BETWEEN 0 AND 10"
    )

    op.create_table(
        "groups",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id",
            sa.UUID(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_groups_tenant_name"),
    )

    op.create_table(
        "user_groups",
        sa.Column(
            "user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "group_id",
            sa.UUID(),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    op.create_table(
        "group_labels",
        sa.Column(
            "group_id",
            sa.UUID(),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "label_id",
            sa.UUID(),
            sa.ForeignKey("access_labels.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    # `label_ids()` resolves the caller's reach before every request does anything else, and
    # it now walks both of these. Unindexed, that is a sequential scan on the hottest path
    # in the product.
    op.create_index("ix_user_groups_user", "user_groups", ["user_id"])
    op.create_index("ix_group_labels_label", "group_labels", ["label_id"])

    for table, condition in {**TENANT_POLICIES, **DERIVED_POLICIES}.items():
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_isolation ON {table} "
            f"USING ({condition}) WITH CHECK ({condition})"
        )
        # The application role owns nothing, which is what makes the policies apply to it.
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO zenith_app")


def downgrade() -> None:
    op.drop_table("group_labels")
    op.drop_table("user_groups")
    op.drop_table("groups")
    op.drop_constraint("ck_access_labels_priority_level", "access_labels", type_="check")
    op.drop_constraint("ck_roles_priority_level", "roles", type_="check")
    op.drop_column("access_labels", "priority_level")
    op.drop_column("roles", "priority_level")
