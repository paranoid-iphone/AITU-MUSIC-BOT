import itertools

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import models
from app.services import bookings as booking_service
from app.services import outbox

_telegram_ids = itertools.count(1000)


@pytest.fixture(autouse=True)
def published(monkeypatch) -> list[dict]:
    """Collect outbox events instead of pushing them to Redis."""
    events: list[dict] = []

    async def _capture(batch: list[dict]) -> None:
        events.extend(batch)

    monkeypatch.setattr(outbox, "publish", _capture)
    return events


@pytest_asyncio.fixture
async def session(monkeypatch) -> AsyncSession:
    # Postgres advisory locks do not exist in SQLite; serialization is not needed in a single-session test.
    async def _no_lock(_session) -> None:
        return None

    monkeypatch.setattr(booking_service, "_lock_bookings", _no_lock)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        for slug, title in models.GROUP_SLUGS:
            db.add(models.Group(slug=slug, title=title))
        for key, value in {
            "weekly_booking_limit_minutes": "240",
            "daily_booking_limit_minutes": "0",
            "booking_window_start_minutes": "480",
            "booking_window_end_minutes": "1260",
        }.items():
            db.add(models.ClubSetting(key=key, value=value))
        for day in range(7):
            db.add(models.AvailableDay(day_of_week=day, is_enabled=True))
        await db.commit()
        yield db
    await engine.dispose()


@pytest_asyncio.fixture
async def make_user(session):
    async def factory(role: str = "user", status: str = "approved", group_slug: str | None = "music", **extra):
        group_id = None
        if group_slug is not None:
            from sqlalchemy import select

            group_id = (await session.execute(select(models.Group.id).where(models.Group.slug == group_slug))).scalar_one()
        telegram_id = next(_telegram_ids)
        user = models.User(
            telegram_id=telegram_id,
            telegram_username=extra.pop("telegram_username", f"user{telegram_id}"),
            first_name=extra.pop("first_name", "Имя"),
            last_name=extra.pop("last_name", f"Фамилия{telegram_id}"),
            study_group="SE-2401",
            role=role,
            status=status,
            group_id=group_id,
            **extra,
        )
        session.add(user)
        await session.commit()
        return user

    return factory


@pytest_asyncio.fixture
async def make_band(session):
    async def factory(owner, name: str = "Band", group_id: int | None = None):
        from app.services import bands

        band = await bands.create_band(session, owner.telegram_id, name, "owner")
        if group_id is not None:
            band.group_id = group_id
            await session.commit()
        return band

    return factory
