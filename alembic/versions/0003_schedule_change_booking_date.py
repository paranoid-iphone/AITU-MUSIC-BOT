"""Index schedule changes by the date of the rehearsal they touch, so the history can be searched by day.

Existing rows are filled from the booking date their snapshots carry.

Revision ID: 0003_schedule_change_booking_date
Revises: 0002_drop_time_slots
"""

from datetime import date

import sqlalchemy as sa
from alembic import op

revision = "0003_schedule_change_booking_date"
down_revision = "0002_drop_time_slots"
branch_labels = None
depends_on = None

INDEX = "ix_schedule_changes_booking_date"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # A database adopted from the old create_all startup may already have the column.
    if "booking_date" not in {column["name"] for column in inspector.get_columns("schedule_changes")}:
        op.add_column("schedule_changes", sa.Column("booking_date", sa.Date(), nullable=True))
    if INDEX not in {index["name"] for index in inspector.get_indexes("schedule_changes")}:
        op.create_index(INDEX, "schedule_changes", ["booking_date"])

    changes = sa.table(
        "schedule_changes",
        sa.column("id", sa.Integer),
        sa.column("booking_date", sa.Date),
        sa.column("before", sa.JSON),
        sa.column("after", sa.JSON),
    )
    rows = bind.execute(sa.select(changes.c.id, changes.c.before, changes.c.after).where(changes.c.booking_date.is_(None))).all()
    for change_id, before, after in rows:
        iso = (after or {}).get("date") or (before or {}).get("date")
        if iso:
            bind.execute(changes.update().where(changes.c.id == change_id).values(booking_date=date.fromisoformat(iso)))


def downgrade() -> None:
    with op.batch_alter_table("schedule_changes") as batch:
        batch.drop_index(INDEX)
        batch.drop_column("booking_date")
