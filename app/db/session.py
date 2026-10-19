import asyncio
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db import models
from app.db.migrate import upgrade_to_head

engine = create_async_engine(settings.database_url, echo=settings.environment == "local")
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# Defaults a fresh club starts with; existing values are never overwritten.
DEFAULT_CLUB_SETTINGS = {
    "weekly_booking_limit_minutes": "240",
    "daily_booking_limit_minutes": "0",
    "booking_window_start_minutes": "480",
    "booking_window_end_minutes": "1260",
    "personal_bookings_enabled": "1",
    "max_future_bookings_per_band": "0",
}


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def prepare_database() -> None:
    """Bring the schema to the latest Alembic revision, then make sure the reference rows exist."""
    await asyncio.to_thread(upgrade_to_head)
    await seed_reference_data()


async def seed_reference_data() -> None:
    """Rows the app expects (not schema); idempotent, so it runs on every start."""
    async with engine.begin() as conn:
        for key, value in DEFAULT_CLUB_SETTINGS.items():
            await conn.execute(
                text("INSERT INTO club_settings (key, value) VALUES (:key, :value) ON CONFLICT (key) DO NOTHING"),
                {"key": key, "value": value},
            )
        for slug, title in models.GROUP_SLUGS:
            await conn.execute(
                text("INSERT INTO groups (slug, title) VALUES (:slug, :title) ON CONFLICT (slug) DO NOTHING"),
                {"slug": slug, "title": title},
            )
        for admin_id in settings.initial_admin_ids:
            await conn.execute(
                text("UPDATE users SET role = 'admin', status = 'approved' WHERE telegram_id = :telegram_id"),
                {"telegram_id": admin_id},
            )
