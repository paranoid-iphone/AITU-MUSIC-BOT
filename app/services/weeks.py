"""Planning-week helpers (weeks start on Monday and are allocated once, on the Sunday before they begin)."""
from datetime import date, datetime, time, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import SchedulingWeek, WeekStatus


def week_start_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def next_week_start(day: date) -> date:
    """The Monday after `day` (tomorrow, when `day` is a Sunday)."""
    return day + timedelta(days=7 - day.weekday())


def allocation_time() -> time:
    try:
        hours, minutes = settings.allocation_time.split(":")
        return time(int(hours), int(minutes))
    except (ValueError, AttributeError):
        return time(18, 0)


def allocation_deadline(week_start: date) -> datetime:
    """Requests for a week close on the preceding Sunday at the allocation time."""
    return datetime.combine(week_start - timedelta(days=1), allocation_time())


async def get_week(session: AsyncSession, week_start: date) -> SchedulingWeek | None:
    return await session.get(SchedulingWeek, week_start)


async def collecting_week(session: AsyncSession, now: datetime) -> date | None:
    """The week currently accepting requests, or None if its allocation already ran or the deadline passed."""
    week = next_week_start(now.date())
    row = await get_week(session, week)
    if row is not None and row.status == WeekStatus.PUBLISHED.value:
        return None
    if now >= allocation_deadline(week):
        return None
    return week


async def is_date_open_for_booking(session: AsyncSession, target: date, now: datetime) -> bool:
    """Manual booking rule: days of an upcoming week stay closed until that week's schedule is published."""
    if not settings.weekly_planning_enabled:
        return True
    if target < next_week_start(now.date()):
        return True
    row = await get_week(session, week_start_of(target))
    return row is not None and row.status == WeekStatus.PUBLISHED.value
