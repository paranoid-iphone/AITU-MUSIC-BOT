"""Roles with a permission catalog: roles, their permissions and who holds them (for the club or for one group).

The three roles a club starts with reproduce what admin, DSVR and group admin could do before. Existing admins, DSVR
and group admins are moved into assignments; the old `users.role` column and `group_admins` table stay until a later
revision drops them, so the code can move over in steps.

Frozen on purpose: the role names and permission keys are copied here instead of imported.

Revision ID: 0004_roles
Revises: 0003_schedule_change_booking_date
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_roles"
down_revision = "0003_schedule_change_booking_date"
branch_labels = None
depends_on = None

ADMIN = "Админ"
# The admin role holds every permission implicitly, so it has no rows here.
DEFAULT_ROLES = {
    "ДСВР": ("bookings.priority", "bookings.manage", "schedule.history"),
    "Админ группы": ("users.moderate", "bands.manage", "bookings.manage", "schedule.history"),
}


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    # A database adopted from the old create_all startup may already have the tables.
    if "roles" not in existing:
        op.create_table(
            "roles",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=60), nullable=False),
            sa.Column("is_system", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name"),
        )
    if "role_permissions" not in existing:
        op.create_table(
            "role_permissions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("role_id", sa.Integer(), nullable=False),
            sa.Column("permission", sa.String(length=60), nullable=False),
            sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("role_id", "permission", name="uq_role_permission"),
        )
    if "role_assignments" not in existing:
        op.create_table(
            "role_assignments",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("role_id", sa.Integer(), nullable=False),
            sa.Column("group_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["group_id"], ["groups.id"]),
            sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_role_assignments_user_id"), "role_assignments", ["user_id"])
        op.create_index(op.f("ix_role_assignments_role_id"), "role_assignments", ["role_id"])

    roles = sa.table(
        "roles",
        sa.column("id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("is_system", sa.Boolean),
        sa.column("created_at", sa.DateTime),
    )
    permissions = sa.table("role_permissions", sa.column("role_id", sa.Integer), sa.column("permission", sa.String))
    assignments = sa.table(
        "role_assignments",
        sa.column("user_id", sa.Integer),
        sa.column("role_id", sa.Integer),
        sa.column("group_id", sa.Integer),
        sa.column("created_at", sa.DateTime),
    )
    now = sa.func.current_timestamp()

    role_ids: dict[str, int] = {}
    for name, is_system in ((ADMIN, True), *((name, False) for name in DEFAULT_ROLES)):
        found = bind.execute(sa.select(roles.c.id).where(roles.c.name == name)).scalar()
        if found is None:
            bind.execute(roles.insert().values(name=name, is_system=is_system, created_at=now))
            found = bind.execute(sa.select(roles.c.id).where(roles.c.name == name)).scalar_one()
            for permission in DEFAULT_ROLES.get(name, ()):
                bind.execute(permissions.insert().values(role_id=found, permission=permission))
        role_ids[name] = found

    # Move today's staff over; the legacy columns stay as they are.
    for legacy_role, role_name in (("admin", ADMIN), ("dsvr", "ДСВР")):
        bind.execute(
            sa.text(
                "INSERT INTO role_assignments (user_id, role_id, group_id, created_at) "
                "SELECT id, :role_id, NULL, CURRENT_TIMESTAMP FROM users WHERE role = :legacy"
            ),
            {"role_id": role_ids[role_name], "legacy": legacy_role},
        )
    bind.execute(
        sa.text(
            "INSERT INTO role_assignments (user_id, role_id, group_id, created_at) "
            "SELECT user_id, :role_id, group_id, CURRENT_TIMESTAMP FROM group_admins"
        ),
        {"role_id": role_ids["Админ группы"]},
    )


def downgrade() -> None:
    op.drop_table("role_assignments")
    op.drop_table("role_permissions")
    op.drop_table("roles")
