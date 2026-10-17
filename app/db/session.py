from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db import models

engine = create_async_engine(settings.database_url, echo=settings.environment == "local")
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def create_db_and_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'pending' NOT NULL"))
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS moderation_reason VARCHAR(255)"))
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS approved_by_id INTEGER REFERENCES users(id)"))
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS approved_at TIMESTAMP"))
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS registration_retry_after TIMESTAMP"))
        await conn.execute(text("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS purpose VARCHAR(30) DEFAULT 'self' NOT NULL"))
        await conn.execute(text("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS song_title VARCHAR(150)"))
        await conn.execute(text("ALTER TABLE bookings ALTER COLUMN band_id DROP NOT NULL"))
        await conn.execute(text("ALTER TABLE available_days ADD COLUMN IF NOT EXISTS occupied_by VARCHAR(150)"))
        await conn.execute(text("ALTER TABLE event_applications ADD COLUMN IF NOT EXISTS song_title VARCHAR(150)"))
        await conn.execute(
            text(
                "INSERT INTO club_settings (key, value) VALUES ('weekly_booking_limit_minutes', '240') "
                "ON CONFLICT (key) DO NOTHING"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO club_settings (key, value) VALUES ('daily_booking_limit_minutes', '0') "
                "ON CONFLICT (key) DO NOTHING"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO club_settings (key, value) VALUES ('booking_window_start_minutes', '480') "
                "ON CONFLICT (key) DO NOTHING"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO club_settings (key, value) VALUES ('booking_window_end_minutes', '1260') "
                "ON CONFLICT (key) DO NOTHING"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO club_settings (key, value) VALUES ('personal_bookings_enabled', '1') "
                "ON CONFLICT (key) DO NOTHING"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO club_settings (key, value) VALUES ('max_future_bookings_per_band', '0') "
                "ON CONFLICT (key) DO NOTHING"
            )
        )
        await conn.execute(text("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS source VARCHAR(20) DEFAULT 'manual' NOT NULL"))
        await conn.execute(text("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS request_id INTEGER"))
        await conn.execute(text("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS cancelled_by_id INTEGER REFERENCES users(id)"))
        await conn.execute(text("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS cancel_reason VARCHAR(255)"))
        await conn.execute(text("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS uq_booking_slot"))
        await conn.execute(text("UPDATE bookings SET source = 'staff' WHERE purpose = 'staff' AND source = 'manual'"))
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS group_id INTEGER REFERENCES groups(id)"))
        await conn.execute(text("ALTER TABLE bands ADD COLUMN IF NOT EXISTS group_id INTEGER REFERENCES groups(id)"))
        await conn.execute(text("ALTER TABLE bands ADD COLUMN IF NOT EXISTS responsible_user_id INTEGER REFERENCES users(id)"))
        for slug, title in models.GROUP_SLUGS:
            await conn.execute(
                text("INSERT INTO groups (slug, title) VALUES (:slug, :title) ON CONFLICT (slug) DO NOTHING"),
                {"slug": slug, "title": title},
            )
        # One-time backfill: everything that existed before groups were introduced belongs to the music club.
        done = await conn.execute(text("SELECT 1 FROM club_settings WHERE key = 'groups_backfilled'"))
        if done.first() is None:
            await conn.execute(text("UPDATE users SET group_id = (SELECT id FROM groups WHERE slug = 'music') WHERE group_id IS NULL"))
            await conn.execute(text("UPDATE bands SET group_id = (SELECT id FROM groups WHERE slug = 'music') WHERE group_id IS NULL"))
            await conn.execute(text("UPDATE bands SET responsible_user_id = creator_id WHERE responsible_user_id IS NULL"))
            await conn.execute(text("INSERT INTO club_settings (key, value) VALUES ('groups_backfilled', '1')"))
        for admin_id in settings.initial_admin_ids:
            await conn.execute(
                text("UPDATE users SET role = 'admin', status = 'approved' WHERE telegram_id = :telegram_id"),
                {"telegram_id": admin_id},
            )
