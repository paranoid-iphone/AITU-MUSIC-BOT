from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import AvailableDay, BandMember, Booking, BookingStatus, ClubSetting, TimeSlot
from app.services.errors import ConflictError, ForbiddenError, NotFoundError
from app.services.users import get_user_by_telegram_id, require_admin, require_approved


async def seed_default_schedule(session: AsyncSession) -> None:
    existing = await session.execute(select(AvailableDay))
    if existing.scalars().first() is not None:
        return
    for day in range(7):
        session.add(AvailableDay(day_of_week=day, is_enabled=day < 6))
    for day in range(6):
        for start_hour in (18, 19, 20):
            session.add(TimeSlot(day_of_week=day, start_time=time(start_hour), end_time=time(start_hour + 1)))
    await session.commit()


def _time_to_minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _minutes_to_time(value: int) -> time:
    return time(value // 60, value % 60)


def _duration_minutes(start_time: time, end_time: time) -> int:
    return _time_to_minutes(end_time) - _time_to_minutes(start_time)


def _overlaps(start_time: time, end_time: time, booked_start: time, booked_end: time) -> bool:
    return start_time < booked_end and end_time > booked_start


async def get_weekly_booking_limit_minutes(session: AsyncSession) -> int:
    result = await session.execute(select(ClubSetting).where(ClubSetting.key == "weekly_booking_limit_minutes"))
    setting = result.scalar_one_or_none()
    if setting is None:
        setting = ClubSetting(key="weekly_booking_limit_minutes", value="240")
        session.add(setting)
        await session.commit()
        return 240
    try:
        return int(setting.value)
    except ValueError:
        return 240


async def get_daily_booking_limit_minutes(session: AsyncSession) -> int:
    result = await session.execute(select(ClubSetting).where(ClubSetting.key == "daily_booking_limit_minutes"))
    setting = result.scalar_one_or_none()
    if setting is None:
        setting = ClubSetting(key="daily_booking_limit_minutes", value="0")
        session.add(setting)
        await session.commit()
        return 0
    try:
        return int(setting.value)
    except ValueError:
        return 0


async def get_booking_window_minutes(session: AsyncSession) -> tuple[int, int]:
    result = await session.execute(
        select(ClubSetting).where(ClubSetting.key.in_(["booking_window_start_minutes", "booking_window_end_minutes"]))
    )
    values = {item.key: item.value for item in result.scalars()}
    start = int(values.get("booking_window_start_minutes", "480"))
    end = int(values.get("booking_window_end_minutes", "1260"))
    if start < 0 or end > 24 * 60 or start >= end:
        return 480, 1260
    return start, end


async def list_available_slots(session: AsyncSession, target_date: date, duration_minutes: int = 60, band_id: int | None = None) -> list[dict]:
    if duration_minutes <= 0 or duration_minutes % 30 != 0:
        return []
    now = datetime.now()
    if band_id is not None:
        weekly_limit = await get_weekly_booking_limit_minutes(session)
        used_minutes = await get_band_booked_minutes_in_window(session, band_id, now)
        if used_minutes + duration_minutes > weekly_limit:
            return []
        daily_limit = await get_daily_booking_limit_minutes(session)
        if daily_limit > 0:
            used_day_minutes = await get_band_booked_minutes_on_date(session, band_id, target_date)
            if used_day_minutes + duration_minutes > daily_limit:
                return []
    if target_date < now.date() or target_date > (now + timedelta(days=7)).date():
        return []
    day = target_date.weekday()
    day_result = await session.execute(select(AvailableDay).where(AvailableDay.day_of_week == day, AvailableDay.is_enabled.is_(True)))
    if day_result.scalar_one_or_none() is None:
        return []
    booked_result = await session.execute(
        select(Booking).where(Booking.booking_date == target_date, Booking.status == BookingStatus.CONFIRMED.value)
    )
    booked = [(item.start_time, item.end_time) for item in booked_result.scalars()]
    window_end = now + timedelta(days=7)
    window_start, window_finish = await get_booking_window_minutes(session)
    result = []
    generated_id = 1
    start = window_start
    while start + duration_minutes <= window_finish:
        slot_start = _minutes_to_time(start)
        slot_end = _minutes_to_time(start + duration_minutes)
        slot_start_dt = datetime.combine(target_date, slot_start)
        if slot_start_dt <= now or slot_start_dt > window_end:
            start += 30
            continue
        if any(_overlaps(slot_start, slot_end, booked_start, booked_end) for booked_start, booked_end in booked):
            start += 30
            continue
        result.append(
            {
                "id": generated_id,
                "day_of_week": day,
                "start_time": slot_start,
                "end_time": slot_end,
                "is_enabled": True,
            }
        )
        generated_id += 1
        start += 30
    return result


async def get_band_booked_minutes_in_window(session: AsyncSession, band_id: int, now: datetime) -> int:
    window_end = now + timedelta(days=7)
    result = await session.execute(
        select(Booking).where(
            Booking.band_id == band_id,
            Booking.status == BookingStatus.CONFIRMED.value,
            Booking.booking_date >= now.date(),
            Booking.booking_date <= window_end.date(),
        )
    )
    total = 0
    for booking in result.scalars():
        booking_start = datetime.combine(booking.booking_date, booking.start_time)
        if booking_start <= now or booking_start > window_end:
            continue
        total += _duration_minutes(booking.start_time, booking.end_time)
    return total


async def get_band_booked_minutes_on_date(session: AsyncSession, band_id: int, target_date: date) -> int:
    result = await session.execute(
        select(Booking).where(
            Booking.band_id == band_id,
            Booking.status == BookingStatus.CONFIRMED.value,
            Booking.booking_date == target_date,
        )
    )
    return sum(_duration_minutes(booking.start_time, booking.end_time) for booking in result.scalars())


async def get_booking_limit_status(session: AsyncSession, telegram_id: int, band_id: int, target_date: date | None = None) -> dict:
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_approved(user)
    member_result = await session.execute(select(BandMember).where(BandMember.band_id == band_id, BandMember.user_id == user.id))
    if member_result.scalar_one_or_none() is None:
        raise ForbiddenError("You are not a member of this band")
    now = datetime.now()
    limit = await get_weekly_booking_limit_minutes(session)
    used = await get_band_booked_minutes_in_window(session, band_id, now)
    remaining = max(0, limit - used)
    daily_limit = await get_daily_booking_limit_minutes(session)
    daily_used = await get_band_booked_minutes_on_date(session, band_id, target_date) if target_date is not None else 0
    daily_remaining = max(0, daily_limit - daily_used) if daily_limit > 0 else 0
    return {
        "weekly_limit_minutes": limit,
        "used_minutes": used,
        "remaining_minutes": remaining,
        "daily_limit_minutes": daily_limit,
        "daily_used_minutes": daily_used,
        "daily_remaining_minutes": daily_remaining,
    }


async def create_booking(
    session: AsyncSession,
    telegram_id: int,
    band_id: int,
    booking_date: date,
    start_time: time,
    end_time: time,
    purpose: str,
    song_title: str | None,
) -> Booking:
    from app.services.audit import log_action

    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_approved(user)
    member_result = await session.execute(select(BandMember).where(BandMember.band_id == band_id, BandMember.user_id == user.id))
    if member_result.scalar_one_or_none() is None:
        raise ForbiddenError("You are not a member of this band")
    duration = _duration_minutes(start_time, end_time)
    if duration <= 0 or duration % 30 != 0:
        raise ConflictError("Booking duration must be a positive 30-minute step")
    slots = await list_available_slots(session, booking_date, duration, band_id)
    if not any(slot["start_time"] == start_time and slot["end_time"] == end_time for slot in slots):
        raise ConflictError("Slot is not available")
    now = datetime.now()
    weekly_limit = await get_weekly_booking_limit_minutes(session)
    used_minutes = await get_band_booked_minutes_in_window(session, band_id, now)
    if used_minutes + duration > weekly_limit:
        raise ConflictError(f"Weekly booking limit exceeded: {weekly_limit / 60:g}h per band")
    booking = Booking(
        band_id=band_id,
        created_by_id=user.id,
        booking_date=booking_date,
        start_time=start_time,
        end_time=end_time,
        purpose=purpose,
        song_title=song_title,
    )
    session.add(booking)
    await session.flush()
    await log_action(
        session,
        user.id,
        "booking.create",
        "booking",
        booking.id,
        f"Создал бронь: коллектив #{band_id}, {booking_date.isoformat()} {start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}, {song_title or 'песня не указана'}",
    )
    await session.commit()
    await session.refresh(booking)
    return booking


async def create_staff_booking(
    session: AsyncSession,
    telegram_id: int,
    booking_date: date,
    start_time: time,
    end_time: time,
    title: str,
) -> Booking:
    from app.services.audit import log_action

    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    duration = _duration_minutes(start_time, end_time)
    if duration <= 0 or duration % 30 != 0:
        raise ConflictError("Booking duration must be a positive 30-minute step")
    slots = await list_available_slots(session, booking_date, duration)
    if not any(slot["start_time"] == start_time and slot["end_time"] == end_time for slot in slots):
        raise ConflictError("Slot is not available")
    booking = Booking(
        band_id=None,
        created_by_id=user.id,
        booking_date=booking_date,
        start_time=start_time,
        end_time=end_time,
        purpose="staff",
        song_title=title,
    )
    session.add(booking)
    await session.flush()
    await log_action(
        session,
        user.id,
        "booking.staff_create",
        "booking",
        booking.id,
        f"Занял кабинет: {booking_date.isoformat()} {start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}, {title}",
    )
    await session.commit()
    await session.refresh(booking)
    return booking


async def delete_booking(session: AsyncSession, telegram_id: int, booking_id: int) -> dict:
    from app.services.audit import log_action

    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    result = await session.execute(
        select(Booking)
        .where(Booking.id == booking_id)
        .options(selectinload(Booking.band), selectinload(Booking.created_by))
    )
    booking = result.scalar_one_or_none()
    if booking is None:
        raise NotFoundError("Booking not found")
    band_name = booking.band.name if booking.band is not None else "Стафф / кабинет"
    created_by = (
        f"{booking.created_by.last_name or ''} {booking.created_by.first_name or ''}".strip()
        or booking.created_by.telegram_username
        or str(booking.created_by.telegram_id)
    )
    deleted = {
        "id": booking.id,
        "band_name": band_name,
        "created_by": created_by,
        "booking_date": booking.booking_date.isoformat(),
        "start_time": booking.start_time.isoformat(),
        "end_time": booking.end_time.isoformat(),
        "purpose": booking.purpose,
        "song_title": booking.song_title,
    }
    await log_action(
        session,
        user.id,
        "booking.delete",
        "booking",
        booking.id,
        f"Удалил бронь: {band_name}, {booking.booking_date.isoformat()} {booking.start_time.strftime('%H:%M')}-{booking.end_time.strftime('%H:%M')}",
    )
    await session.delete(booking)
    await session.commit()
    return deleted


async def list_bookings(session: AsyncSession, telegram_id: int | None = None) -> list[Booking]:
    stmt = select(Booking).options(selectinload(Booking.band)).order_by(Booking.booking_date, Booking.start_time)
    if telegram_id is not None:
        user = await get_user_by_telegram_id(session, telegram_id)
        if user is None:
            return []
        stmt = stmt.join(BandMember, BandMember.band_id == Booking.band_id).where(BandMember.user_id == user.id)
    result = await session.execute(stmt)
    return list(result.scalars().unique())


async def list_schedule(session: AsyncSession) -> list[dict]:
    now = datetime.now()
    window_end = now + timedelta(days=7)
    result = await session.execute(
        select(Booking)
        .where(
            Booking.status == BookingStatus.CONFIRMED.value,
            Booking.booking_date >= now.date(),
            Booking.booking_date <= window_end.date(),
        )
        .options(selectinload(Booking.band), selectinload(Booking.created_by))
        .order_by(Booking.booking_date, Booking.start_time)
    )
    rows = []
    for booking in result.scalars().unique():
        slot_start = datetime.combine(booking.booking_date, booking.start_time)
        if slot_start <= now or slot_start > window_end:
            continue
        created_by = booking.created_by
        rows.append(
            {
                "id": booking.id,
                "band_name": booking.band.name if booking.band is not None else "Стафф / кабинет",
                "created_by": f"{created_by.last_name or ''} {created_by.first_name or ''}".strip()
                or created_by.telegram_username
                or str(created_by.telegram_id),
                "booking_date": booking.booking_date,
                "start_time": booking.start_time,
                "end_time": booking.end_time,
                "purpose": booking.purpose,
                "song_title": booking.song_title,
                "status": booking.status,
            }
        )
    return rows
