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
        for admin_id in settings.initial_admin_ids:
            await conn.execute(
                text("UPDATE users SET role = 'admin', status = 'approved' WHERE telegram_id = :telegram_id"),
                {"telegram_id": admin_id},
            )
