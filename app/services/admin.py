from datetime import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AvailableDay, ClubSetting
from app.services.errors import ConflictError, NotFoundError
from app.services.outbox import queue_schedule_changed
from app.services.users import get_user_by_telegram_id, require_admin


def _time_to_minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _minutes_to_hhmm(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


async def update_available_day(
    session: AsyncSession,
    telegram_id: int,
    day_of_week: int,
    is_enabled: bool,
    occupied_by: str | None = None,
) -> AvailableDay:
    from app.services.audit import log_action

    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    result = await session.execute(select(AvailableDay).where(AvailableDay.day_of_week == day_of_week))
    day = result.scalar_one_or_none()
    note = None if is_enabled else ((occupied_by or "").strip()[:150] or None)
    if day is None:
        day = AvailableDay(day_of_week=day_of_week, is_enabled=is_enabled, occupied_by=note)
        session.add(day)
    else:
        day.is_enabled = is_enabled
        day.occupied_by = note
    await log_action(
        session,
        user.id,
        "schedule.day.update",
        "available_day",
        day_of_week,
        f"{'Включил' if is_enabled else 'Выключил'} день недели: {day_of_week}"
        + ("" if is_enabled else f", занято: {note}"),
    )
    queue_schedule_changed(session)
    await session.commit()
    await session.refresh(day)
    return day


async def get_weekly_booking_limit(session: AsyncSession, telegram_id: int) -> dict:
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    result = await session.execute(select(ClubSetting).where(ClubSetting.key == "weekly_booking_limit_minutes"))
    setting = result.scalar_one_or_none()
    minutes = int(setting.value) if setting is not None else 240
    return {"weekly_booking_limit_minutes": minutes, "weekly_booking_limit_hours": minutes / 60}


async def update_weekly_booking_limit(session: AsyncSession, telegram_id: int, hours: float, commit: bool = True) -> dict:
    from app.services.audit import log_action

    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    minutes = int(hours * 60)
    result = await session.execute(select(ClubSetting).where(ClubSetting.key == "weekly_booking_limit_minutes"))
    setting = result.scalar_one_or_none()
    if setting is None:
        setting = ClubSetting(key="weekly_booking_limit_minutes", value=str(minutes))
        session.add(setting)
    else:
        setting.value = str(minutes)
    await log_action(
        session,
        user.id,
        "settings.weekly_limit.update",
        "club_setting",
        "weekly_booking_limit_minutes",
        f"Обновил недельный лимит: {minutes / 60:g} ч",
    )
    if commit:
        await session.commit()
    return {"weekly_booking_limit_minutes": minutes, "weekly_booking_limit_hours": minutes / 60}


async def get_daily_booking_limit(session: AsyncSession, telegram_id: int) -> dict:
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    result = await session.execute(select(ClubSetting).where(ClubSetting.key == "daily_booking_limit_minutes"))
    setting = result.scalar_one_or_none()
    minutes = int(setting.value) if setting is not None else 0
    return {"daily_booking_limit_minutes": minutes, "daily_booking_limit_hours": minutes / 60}


async def update_daily_booking_limit(session: AsyncSession, telegram_id: int, hours: float, commit: bool = True) -> dict:
    from app.services.audit import log_action

    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    minutes = int(hours * 60)
    result = await session.execute(select(ClubSetting).where(ClubSetting.key == "daily_booking_limit_minutes"))
    setting = result.scalar_one_or_none()
    if setting is None:
        setting = ClubSetting(key="daily_booking_limit_minutes", value=str(minutes))
        session.add(setting)
    else:
        setting.value = str(minutes)
    await log_action(
        session,
        user.id,
        "settings.daily_limit.update",
        "club_setting",
        "daily_booking_limit_minutes",
        f"Обновил дневной лимит: {'выключен' if minutes == 0 else f'{minutes / 60:g} ч'}",
    )
    if commit:
        await session.commit()
    return {"daily_booking_limit_minutes": minutes, "daily_booking_limit_hours": minutes / 60}


async def get_max_future_bookings(session: AsyncSession, telegram_id: int) -> dict:
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    from app.services.bookings import get_max_future_bookings_per_band

    return {"max_future_bookings_per_band": await get_max_future_bookings_per_band(session)}


async def update_max_future_bookings(session: AsyncSession, telegram_id: int, count: int, commit: bool = True) -> dict:
    from app.services.audit import log_action

    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    if count < 0:
        raise ConflictError("Limit must not be negative")
    result = await session.execute(select(ClubSetting).where(ClubSetting.key == "max_future_bookings_per_band"))
    setting = result.scalar_one_or_none()
    if setting is None:
        session.add(ClubSetting(key="max_future_bookings_per_band", value=str(count)))
    else:
        setting.value = str(count)
    await log_action(
        session,
        user.id,
        "settings.max_future_bookings.update",
        "club_setting",
        "max_future_bookings_per_band",
        f"Обновил лимит будущих броней на номер: {'выключен' if count == 0 else count}",
    )
    if commit:
        await session.commit()
    return {"max_future_bookings_per_band": count}


async def update_limits(
    session: AsyncSession,
    telegram_id: int,
    weekly_hours: float | None = None,
    daily_hours: float | None = None,
    max_future_bookings: int | None = None,
) -> None:
    """Change the given limits together: all of them are stored or none, and a day may not be longer than a week."""
    weekly = (await get_weekly_booking_limit(session, telegram_id))["weekly_booking_limit_minutes"]
    daily = (await get_daily_booking_limit(session, telegram_id))["daily_booking_limit_minutes"]
    if weekly_hours is not None:
        weekly = int(weekly_hours * 60)
    if daily_hours is not None:
        daily = int(daily_hours * 60)
    if daily and daily > weekly:
        raise ConflictError("Daily limit cannot be longer than the weekly limit")
    if weekly_hours is not None:
        await update_weekly_booking_limit(session, telegram_id, weekly_hours, commit=False)
    if daily_hours is not None:
        await update_daily_booking_limit(session, telegram_id, daily_hours, commit=False)
    if max_future_bookings is not None:
        await update_max_future_bookings(session, telegram_id, max_future_bookings, commit=False)
    await session.commit()


async def get_booking_window(session: AsyncSession, telegram_id: int) -> dict:
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    result = await session.execute(
        select(ClubSetting).where(ClubSetting.key.in_(["booking_window_start_minutes", "booking_window_end_minutes"]))
    )
    values = {item.key: item.value for item in result.scalars()}
    start = int(values.get("booking_window_start_minutes", "480"))
    end = int(values.get("booking_window_end_minutes", "1260"))
    return {
        "start_minutes": start,
        "end_minutes": end,
        "start_time": _minutes_to_hhmm(start),
        "end_time": _minutes_to_hhmm(end),
    }


async def update_booking_window(session: AsyncSession, telegram_id: int, start_time: time, end_time: time) -> dict:
    from app.services.audit import log_action

    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    start = _time_to_minutes(start_time)
    end = _time_to_minutes(end_time)
    if start >= end:
        raise ConflictError("Start time must be earlier than end time")
    if start % 30 or end % 30:
        raise ConflictError("Room hours must start and end on the half hour")
    for key, value in {
        "booking_window_start_minutes": start,
        "booking_window_end_minutes": end,
    }.items():
        result = await session.execute(select(ClubSetting).where(ClubSetting.key == key))
        setting = result.scalar_one_or_none()
        if setting is None:
            session.add(ClubSetting(key=key, value=str(value)))
        else:
            setting.value = str(value)
    await log_action(
        session,
        user.id,
        "schedule.window.update",
        "club_setting",
        "booking_window",
        f"Обновил часы кабинета: {_minutes_to_hhmm(start)}-{_minutes_to_hhmm(end)}",
    )
    queue_schedule_changed(session)
    await session.commit()
    return {
        "start_minutes": start,
        "end_minutes": end,
        "start_time": _minutes_to_hhmm(start),
        "end_time": _minutes_to_hhmm(end),
    }


async def get_personal_bookings_enabled(session: AsyncSession, telegram_id: int | None = None, require_admin_access: bool = False) -> dict:
    if require_admin_access:
        if telegram_id is None:
            raise NotFoundError("Admin not found")
        user = await get_user_by_telegram_id(session, telegram_id)
        if user is None:
            raise NotFoundError("Admin not found")
        require_admin(user)
    result = await session.execute(select(ClubSetting).where(ClubSetting.key == "personal_bookings_enabled"))
    setting = result.scalar_one_or_none()
    is_enabled = setting is None or setting.value == "1"
    return {"personal_bookings_enabled": is_enabled}


async def update_personal_bookings_enabled(session: AsyncSession, telegram_id: int, is_enabled: bool) -> dict:
    from app.services.audit import log_action

    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    result = await session.execute(select(ClubSetting).where(ClubSetting.key == "personal_bookings_enabled"))
    setting = result.scalar_one_or_none()
    value = "1" if is_enabled else "0"
    if setting is None:
        session.add(ClubSetting(key="personal_bookings_enabled", value=value))
    else:
        setting.value = value
    await log_action(
        session,
        user.id,
        "settings.personal_bookings.update",
        "club_setting",
        "personal_bookings_enabled",
        f"{'Включил' if is_enabled else 'Выключил'} личные репетиции",
    )
    await session.commit()
    return {"personal_bookings_enabled": is_enabled}


async def get_settings(session: AsyncSession, telegram_id: int) -> dict:
    """Everything the Mini App settings tab shows, in one read."""
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    rows = {item.day_of_week: item for item in (await session.execute(select(AvailableDay))).scalars()}
    days = [
        {
            "day_of_week": day,
            "is_enabled": rows[day].is_enabled if day in rows else day < 6,
            "occupied_by": rows[day].occupied_by if day in rows else None,
        }
        for day in range(7)
    ]
    window = await get_booking_window(session, telegram_id)
    weekly = await get_weekly_booking_limit(session, telegram_id)
    daily = await get_daily_booking_limit(session, telegram_id)
    future = await get_max_future_bookings(session, telegram_id)
    personal = await get_personal_bookings_enabled(session)
    return {
        "days": days,
        "window": {"start_time": window["start_time"], "end_time": window["end_time"]},
        "weekly_hours": weekly["weekly_booking_limit_hours"],
        "daily_hours": daily["daily_booking_limit_hours"],
        "max_future_bookings": future["max_future_bookings_per_band"],
        "personal_enabled": personal["personal_bookings_enabled"],
    }
