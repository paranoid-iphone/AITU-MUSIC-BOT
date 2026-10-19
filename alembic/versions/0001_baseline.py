"""Baseline: the schema as it was when Alembic was introduced.

Frozen on purpose: it does not import app.db.models, so later model edits never change it. Databases created by the
old startup code (create_all + ALTER TABLE ... IF NOT EXISTS) are brought up to this schema in place: tables that are
missing are created, the old column fixes are replayed (Postgres only) and nothing existing is dropped.

Revision ID: 0001_baseline
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

GROUPS = (("music", "Мьюзик"), ("dance", "Денс"), ("cheer", "Чирлидерши"))

# Replayed on databases that predate this baseline; each statement is idempotent.
LEGACY_STATEMENTS = (
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'pending' NOT NULL",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS moderation_reason VARCHAR(255)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS approved_by_id INTEGER REFERENCES users(id)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS approved_at TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS registration_retry_after TIMESTAMP",
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS purpose VARCHAR(30) DEFAULT 'self' NOT NULL",
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS song_title VARCHAR(150)",
    "ALTER TABLE bookings ALTER COLUMN band_id DROP NOT NULL",
    "ALTER TABLE available_days ADD COLUMN IF NOT EXISTS occupied_by VARCHAR(150)",
    "ALTER TABLE event_applications ADD COLUMN IF NOT EXISTS song_title VARCHAR(150)",
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS source VARCHAR(20) DEFAULT 'manual' NOT NULL",
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS request_id INTEGER",
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS cancelled_by_id INTEGER REFERENCES users(id)",
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS cancel_reason VARCHAR(255)",
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS repeat_offered_at TIMESTAMP",
    "ALTER TABLE bookings DROP CONSTRAINT IF EXISTS uq_booking_slot",
    "UPDATE bookings SET source = 'staff' WHERE purpose = 'staff' AND source = 'manual'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS group_id INTEGER REFERENCES groups(id)",
    "ALTER TABLE bands ADD COLUMN IF NOT EXISTS group_id INTEGER REFERENCES groups(id)",
    "ALTER TABLE bands ADD COLUMN IF NOT EXISTS responsible_user_id INTEGER REFERENCES users(id)",
)

TABLES_IN_CREATION_ORDER = (
    "available_days",
    "club_settings",
    "groups",
    "outbox_events",
    "scheduling_weeks",
    "time_slots",
    "users",
    "audit_logs",
    "bands",
    "events",
    "group_admins",
    "personal_event_history",
    "band_members",
    "bookings",
    "event_applications",
    "waitlist_entries",
    "availability_requests",
    "event_application_members",
    "move_requests",
    "schedule_changes",
    "availability_windows",
)


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    legacy = "users" in existing

    def create(name: str, *items, indexes: tuple = ()) -> None:
        # Indexes are created with their table only: a table that already existed has the ones create_all gave it.
        if name in existing:
            return
        op.create_table(name, *items)
        for index_name, columns, unique in indexes:
            op.create_index(op.f(index_name), name, columns, unique=unique)

    create(
        "available_days",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("occupied_by", sa.String(length=150), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("day_of_week"),
    )
    create(
        "club_settings",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    create(
        "groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=30), nullable=False),
        sa.Column("title", sa.String(length=80), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    create(
        "outbox_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        indexes=(("ix_outbox_events_published_at", ["published_at"], False),),
    )
    create(
        "scheduling_weeks",
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("generated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("week_start"),
    )
    create(
        "time_slots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("day_of_week", "start_time", "end_time", name="uq_slot"),
    )
    create(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_username", sa.String(length=255), nullable=True),
        sa.Column("telegram_full_name", sa.String(length=255), nullable=True),
        sa.Column("first_name", sa.String(length=100), nullable=True),
        sa.Column("last_name", sa.String(length=100), nullable=True),
        sa.Column("study_group", sa.String(length=50), nullable=True),
        sa.Column("language", sa.String(length=2), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("notifications_enabled", sa.Boolean(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("moderation_reason", sa.String(length=255), nullable=True),
        sa.Column("approved_by_id", sa.Integer(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("registration_retry_after", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["approved_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"]),
        sa.PrimaryKeyConstraint("id"),
        indexes=(("ix_users_telegram_id", ["telegram_id"], True),),
    )
    create(
        "audit_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=False),
        sa.Column("entity_id", sa.String(length=80), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    create(
        "bands",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("invite_code", sa.String(length=30), nullable=False),
        sa.Column("creator_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("responsible_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["creator_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"]),
        sa.ForeignKeyConstraint(["responsible_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        indexes=(
            ("ix_bands_invite_code", ["invite_code"], True),
            ("ix_bands_name", ["name"], False),
        ),
    )
    create(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=150), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("location", sa.String(length=150), nullable=True),
        sa.Column("submission_deadline", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    create(
        "group_admins",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "user_id", name="uq_group_admin"),
    )
    create(
        "personal_event_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=150), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("location", sa.String(length=150), nullable=True),
        sa.Column("role_at_event", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    create(
        "band_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("band_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("instrument_role", sa.String(length=80), nullable=False),
        sa.Column("is_owner", sa.Boolean(), nullable=False),
        sa.Column("notifications_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["band_id"], ["bands.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("band_id", "user_id", name="uq_band_member"),
    )
    create(
        "bookings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("band_id", sa.Integer(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("booking_date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("purpose", sa.String(length=30), nullable=False),
        sa.Column("song_title", sa.String(length=150), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=True),
        sa.Column("cancelled_by_id", sa.Integer(), nullable=True),
        sa.Column("cancel_reason", sa.String(length=255), nullable=True),
        sa.Column("repeat_offered_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["band_id"], ["bands.id"]),
        sa.ForeignKeyConstraint(["cancelled_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    create(
        "event_applications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("band_id", sa.Integer(), nullable=False),
        sa.Column("submitted_by_id", sa.Integer(), nullable=False),
        sa.Column("song_title", sa.String(length=150), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["band_id"], ["bands.id"]),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"]),
        sa.ForeignKeyConstraint(["submitted_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "band_id", name="uq_event_band_application"),
    )
    create(
        "waitlist_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("band_id", sa.Integer(), nullable=True),
        sa.Column("booking_date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["band_id"], ["bands.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        indexes=(
            ("ix_waitlist_entries_band_id", ["band_id"], False),
            ("ix_waitlist_entries_booking_date", ["booking_date"], False),
            ("ix_waitlist_entries_user_id", ["user_id"], False),
        ),
    )
    create(
        "availability_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("band_id", sa.Integer(), nullable=False),
        sa.Column("submitted_by_id", sa.Integer(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(length=30), nullable=False),
        sa.Column("song_title", sa.String(length=150), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["band_id"], ["bands.id"]),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"]),
        sa.ForeignKeyConstraint(["submitted_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        indexes=(("ix_availability_requests_week_start", ["week_start"], False),),
    )
    create(
        "event_application_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("instrument_role", sa.String(length=80), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["event_applications.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("application_id", "user_id", name="uq_application_user"),
    )
    create(
        "move_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=False),
        sa.Column("requester_id", sa.Integer(), nullable=False),
        sa.Column("proposed_date", sa.Date(), nullable=False),
        sa.Column("proposed_start", sa.Time(), nullable=False),
        sa.Column("proposed_end", sa.Time(), nullable=False),
        sa.Column("message", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"]),
        sa.ForeignKeyConstraint(["requester_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["resolved_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        indexes=(
            ("ix_move_requests_booking_id", ["booking_id"], False),
            ("ix_move_requests_requester_id", ["requester_id"], False),
            ("ix_move_requests_status", ["status"], False),
        ),
    )
    create(
        "schedule_changes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=True),
        sa.Column("band_id", sa.Integer(), nullable=True),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("band_name", sa.String(length=150), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sheets_synced_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["band_id"], ["bands.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"]),
        sa.PrimaryKeyConstraint("id"),
        indexes=(
            ("ix_schedule_changes_band_id", ["band_id"], False),
            ("ix_schedule_changes_booking_id", ["booking_id"], False),
            ("ix_schedule_changes_created_at", ["created_at"], False),
            ("ix_schedule_changes_group_id", ["group_id"], False),
        ),
    )
    create(
        "availability_windows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.ForeignKeyConstraint(["request_id"], ["availability_requests.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", "priority", name="uq_request_priority"),
    )

    if legacy and bind.dialect.name == "postgresql":
        _upgrade_legacy(bind)


def _upgrade_legacy(bind) -> None:
    """What create_db_and_tables() used to do on every start, for a database that was created by that code."""
    for statement in LEGACY_STATEMENTS:
        op.execute(statement)
    for slug, title in GROUPS:
        bind.execute(
            sa.text("INSERT INTO groups (slug, title) VALUES (:slug, :title) ON CONFLICT (slug) DO NOTHING"),
            {"slug": slug, "title": title},
        )
    # One-time backfill: everything that existed before groups were introduced belongs to the music club.
    done = bind.execute(sa.text("SELECT 1 FROM club_settings WHERE key = 'groups_backfilled'")).first()
    if done is None:
        op.execute("UPDATE users SET group_id = (SELECT id FROM groups WHERE slug = 'music') WHERE group_id IS NULL")
        op.execute("UPDATE bands SET group_id = (SELECT id FROM groups WHERE slug = 'music') WHERE group_id IS NULL")
        op.execute("UPDATE bands SET responsible_user_id = creator_id WHERE responsible_user_id IS NULL")
        op.execute("INSERT INTO club_settings (key, value) VALUES ('groups_backfilled', '1')")


def downgrade() -> None:
    for name in reversed(TABLES_IN_CREATION_ORDER):
        op.drop_table(name)
