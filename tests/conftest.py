import itertools

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db import models
from app.services import bookings as booking_service
from app.services import outbox, roles

_telegram_ids = itertools.count(1000)


@pytest.fixture(autouse=True)
def published(monkeypatch) -> list[dict]:
    """Collect outbox events instead of pushing them to Redis."""
    events: list[dict] = []

    async def _capture(batch: list[dict]) -> bool:
        events.extend(batch)
        return True

    monkeypatch.setattr(outbox, "publish", _capture)
    return events


@pytest_asyncio.fixture
async def session(monkeypatch) -> AsyncSession:
    # Postgres advisory locks do not exist in SQLite; serialization is not needed in a single-session test.
    async def _no_lock(_session) -> None:
        return None

    monkeypatch.setattr(booking_service, "_lock_bookings", _no_lock)
    from app.services import planning

    monkeypatch.setattr(planning, "_lock_bookings", _no_lock)
    # Weekly planning closes next-week days for manual booking, which would make date-relative tests depend on
    # the weekday they run on. Planning tests switch it on explicitly via the `planning_on` fixture.
    monkeypatch.setattr(settings, "weekly_planning_enabled", False)

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
        await roles.seed_default_roles(db)
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
async def grant(session):
    """grant(user, "ДСВР") / grant(user, "Админ группы", "music"): give a user a role for the club or for one group."""

    async def factory(user, role_name: str, group_slug: str | None = None):
        from sqlalchemy import select

        role_id = (await session.execute(select(models.Role.id).where(models.Role.name == role_name))).scalar_one()
        group_id = None
        if group_slug is not None:
            group_id = (await session.execute(select(models.Group.id).where(models.Group.slug == group_slug))).scalar_one()
        assignment = models.RoleAssignment(user_id=user.id, role_id=role_id, group_id=group_id)
        session.add(assignment)
        await session.commit()
        return assignment

    return factory


@pytest_asyncio.fixture
async def role_holder(session, make_user, grant):
    """role_holder("Модератор", ["users.moderate"], "dance"): a user holding a new custom role, for the club or one group.

    The role is made by a throwaway admin through the service; the assignment is a plain row, so no notification is sent."""

    async def factory(name: str, permission_keys: list[str], group_slug: str | None = None):
        from app.services import roles as role_service

        creator = await make_user()
        await grant(creator, "Админ")
        await role_service.create_role(session, creator.telegram_id, name, permission_keys)
        holder = await make_user()
        await grant(holder, name, group_slug)
        return holder

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


@pytest.fixture
def planning_on(session, monkeypatch):  # depends on `session` so it is applied after the default-off setting
    monkeypatch.setattr(settings, "weekly_planning_enabled", True)


@pytest.fixture
def freeze_now(monkeypatch):
    """Pin the club clock: freeze_now(datetime(2026, 10, 18, 12, 0))."""
    from app.api.routes import webapp
    from app.services import bands, events, move_requests, planning, repeat_offers, users, waitlist

    def _freeze(moment):
        for module in (booking_service, planning, bands, events, webapp, repeat_offers, move_requests, users, waitlist):
            monkeypatch.setattr(module, "now_local", lambda moment=moment: moment)

    return _freeze
