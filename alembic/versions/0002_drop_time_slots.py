"""Drop time_slots: nothing reads it since bookings use free 30-minute steps inside the room hours.

Revision ID: 0002_drop_time_slots
Revises: 0001_baseline
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_drop_time_slots"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("time_slots"):
        op.drop_table("time_slots")


def downgrade() -> None:
    op.create_table(
        "time_slots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("day_of_week", "start_time", "end_time", name="uq_slot"),
    )
