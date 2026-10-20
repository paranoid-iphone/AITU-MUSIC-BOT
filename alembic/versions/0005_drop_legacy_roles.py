"""Drop the legacy role bridge: the `users.role` column and the `group_admins` table.

Admins, DSVR and group admins were moved into role assignments by 0004_roles and every permission check reads them from
there now. Downgrading brings both back empty-handed: `role` defaults to 'user' and `group_admins` has no rows, so the
people who held a role are not restored.

Revision ID: 0005_drop_legacy_roles
Revises: 0004_roles
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_drop_legacy_roles"
down_revision = "0004_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    # A database adopted from the old create_all startup may already lack them.
    if "group_admins" in inspector.get_table_names():
        op.drop_table("group_admins")
    if "role" in {column["name"] for column in inspector.get_columns("users")}:
        with op.batch_alter_table("users") as batch:
            batch.drop_column("role")


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("role", sa.String(length=20), nullable=False, server_default="user"))
    op.create_table(
        "group_admins",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "user_id", name="uq_group_admin"),
    )
