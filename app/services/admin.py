from datetime import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import AvailableDay, ClubSetting, TimeSlot
from app.services.errors import ConflictError, NotFoundError
from app.services.users import get_user_by_telegram_id, require_admin


def _time_to_minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _minutes_to_hhmm(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


async def update_available_day(session: AsyncSession, telegram_id: int, day_of_week: int, is_enabled: bool) -> AvailableDay:
    from app.services.audit import log_action

    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    result = await session.execute(select(AvailableDay).where(AvailableDay.day_of_week == day_of_week))
    day = result.scalar_one_or_none()
    if day is None:
        day = AvailableDay(day_of_week=day_of_week, is_enabled=is_enabled)
        session.add(day)
    else:
        day.is_enabled = is_enabled
    await log_action(
        session,
        user.id,
        "schedule.day.update",
        "available_day",
        day_of_week,
        f"{'Включил' if is_enabled else 'Выключил'} день недели: {day_of_week}",
    )
    await session.commit()
    await session.refresh(day)
    return day


async def create_slot(session: AsyncSession, telegram_id: int, day_of_week: int, start_time: time, end_time: time) -> TimeSlot:
    from app.services.audit import log_action

    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    slot = TimeSlot(day_of_week=day_of_week, start_time=start_time, end_time=end_time)
    session.add(slot)
    await log_action(
        session,
        user.id,
        "schedule.slot.create",
        "time_slot",
        None,
        f"Создал слот: день {day_of_week}, {start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}",
    )
    await session.commit()
    await session.refresh(slot)
    return slot


async def toggle_slot(session: AsyncSession, telegram_id: int, slot_id: int, is_enabled: bool) -> TimeSlot:
    from app.services.audit import log_action

    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    result = await session.execute(select(TimeSlot).where(TimeSlot.id == slot_id))
    slot = result.scalar_one_or_none()
    if slot is None:
        raise NotFoundError("Slot not found")
    slot.is_enabled = is_enabled
    await log_action(
        session,
        user.id,
        "schedule.slot.update",
        "time_slot",
        slot.id,
        f"{'Включил' if is_enabled else 'Выключил'} слот: день {slot.day_of_week}, {slot.start_time.strftime('%H:%M')}-{slot.end_time.strftime('%H:%M')}",
    )
    await session.commit()
    await session.refresh(slot)
    return slot


async def get_weekly_booking_limit(session: AsyncSession, telegram_id: int) -> dict:
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    result = await session.execute(select(ClubSetting).where(ClubSetting.key == "weekly_booking_limit_minutes"))
    setting = result.scalar_one_or_none()
    minutes = int(setting.value) if setting is not None else 240
    return {"weekly_booking_limit_minutes": minutes, "weekly_booking_limit_hours": minutes / 60}


async def update_weekly_booking_limit(session: AsyncSession, telegram_id: int, hours: float) -> dict:
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


async def update_daily_booking_limit(session: AsyncSession, telegram_id: int, hours: float) -> dict:
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
    await session.commit()
    return {"daily_booking_limit_minutes": minutes, "daily_booking_limit_hours": minutes / 60}


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
    await session.commit()
    return {
        "start_minutes": start,
        "end_minutes": end,
        "start_time": _minutes_to_hhmm(start),
        "end_time": _minutes_to_hhmm(end),
    }


async def booking_history(session: AsyncSession, telegram_id: int) -> list[dict]:
    from app.db.models import Booking

    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    result = await session.execute(
        select(Booking)
        .options(selectinload(Booking.band), selectinload(Booking.created_by))
        .order_by(Booking.created_at.desc())
    )
    rows = []
    for booking in result.scalars():
        rows.append(
            {
                "id": booking.id,
                "band_id": booking.band_id,
                "band_name": booking.band.name if booking.band is not None else "Стафф / кабинет",
                "created_by_id": booking.created_by_id,
                "created_by": f"{booking.created_by.last_name or ''} {booking.created_by.first_name or ''}".strip()
                or booking.created_by.telegram_username
                or str(booking.created_by.telegram_id),
                "booking_date": booking.booking_date.isoformat(),
                "start_time": booking.start_time.isoformat(),
                "end_time": booking.end_time.isoformat(),
                "purpose": booking.purpose,
                "song_title": booking.song_title,
                "status": booking.status,
                "created_at": booking.created_at.isoformat(),
            }
        )
    return rows
