"""Rehearsal reminders: a per-user switch and the "already reminded" marks on bookings.

`users.rehearsal_reminders_enabled` is on for everyone, existing users included. `bookings.reminded_evening_at` /
`reminded_soon_at` stay empty for existing bookings, so their upcoming rehearsals are reminded as usual.

Plain ADD / DROP COLUMN, no batch mode: SQLite would rebuild `users`, whose self-reference (`approved_by_id`) the
batch rebuild cannot order.

Revision ID: 0006_rehearsal_reminders
Revises: 0005_drop_legacy_roles
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_rehearsal_reminders"
down_revision = "0005_drop_legacy_roles"
branch_labels = None
depends_on = None


def _columns(inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    # A database adopted from the old create_all startup may already have them.
    if "rehearsal_reminders_enabled" not in _columns(inspector, "users"):
        op.add_column("users", sa.Column("rehearsal_reminders_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    booking_columns = _columns(inspector, "bookings")
    for name in ("reminded_evening_at", "reminded_soon_at"):
        if name not in booking_columns:
            op.add_column("bookings", sa.Column(name, sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("bookings", "reminded_soon_at")
    op.drop_column("bookings", "reminded_evening_at")
    op.drop_column("users", "rehearsal_reminders_enabled")
