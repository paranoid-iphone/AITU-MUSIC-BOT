from datetime import date, datetime, time, timedelta

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.timezone import now_local
from app.db.models import AvailableDay, Band, BandMember, Booking, BookingStatus, ClubSetting, TimeSlot
from app.services.outbox import commit_and_publish
from app.services.permissions import can_manage_schedule, is_dsvr, is_global_admin
from app.services.schedule_history import record_change, snapshot
from app.services.weeks import is_date_open_for_booking
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


def _booking_owner_label(booking: Booking) -> str:
    if booking.band is not None:
        return booking.band.name
    if booking.purpose == "personal":
        created_by = booking.created_by
        name = (
            f"{created_by.last_name or ''} {created_by.first_name or ''}".strip()
            or created_by.telegram_username
            or str(created_by.telegram_id)
        )
        return f"Личная репетиция | {name}"
    return "Стафф / кабинет"


def _person_name(user) -> str:
    return f"{user.last_name or ''} {user.first_name or ''}".strip() or user.telegram_username or str(user.telegram_id)


def _band_info(booking: Booking) -> dict:
    """Group and responsible person shown next to a booking in the schedule table."""
    band = booking.band
    if band is None:
        return {"group_title": None, "responsible_name": None, "responsible_username": None}
    responsible = band.responsible
    return {
        "group_title": band.group.title if band.group is not None else None,
        "responsible_name": _person_name(responsible) if responsible is not None else None,
        "responsible_username": responsible.telegram_username if responsible is not None else None,
    }


def _booking_load_options():
    return (
        selectinload(Booking.band).selectinload(Band.responsible),
        selectinload(Booking.band).selectinload(Band.group),
        selectinload(Booking.created_by),
    )


async def _can_manage_booking(session: AsyncSession, user, booking: Booking) -> bool:
    return await can_manage_schedule(session, user, booking)


def _ensure_booking_not_past(booking: Booking) -> None:
    if datetime.combine(booking.booking_date, booking.end_time) <= now_local():
        raise ConflictError("Past bookings cannot be changed")


_BOOKING_LOCK_KEY = 7351001


async def _lock_bookings(session: AsyncSession) -> None:
    """Serialize booking writes so overlap checks and inserts are atomic (released on commit/rollback)."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _BOOKING_LOCK_KEY})


async def _ensure_booking_time_available(
    session: AsyncSession,
    booking_date: date,
    start_time: time,
    end_time: time,
    exclude_booking_id: int | None = None,
    privileged: bool = False,
) -> None:
    now = now_local()
    duration = _duration_minutes(start_time, end_time)
    if duration <= 0 or duration % 30 != 0:
        raise ConflictError("Booking duration must be a positive 30-minute step")
    if booking_date < now.date() or booking_date > (now + timedelta(days=7)).date():
        raise ConflictError("Booking date must be within the next 7 days")
    if not privileged and not await is_date_open_for_booking(session, booking_date, now):
        raise ConflictError("The schedule for that week is not published yet: send a weekly request instead")
    start_dt = datetime.combine(booking_date, start_time)
    if start_dt <= now or start_dt > now + timedelta(days=7):
        raise ConflictError("Booking must start in the future and within the next 7 days")
    window_start, window_finish = await get_booking_window_minutes(session)
    if _time_to_minutes(start_time) < window_start or _time_to_minutes(end_time) > window_finish:
        raise ConflictError("Booking is outside room working hours")
    day_result = await session.execute(select(AvailableDay).where(AvailableDay.day_of_week == booking_date.weekday(), AvailableDay.is_enabled.is_(True)))
    if day_result.scalar_one_or_none() is None:
        raise ConflictError("This day is not available")
    stmt = select(Booking).where(Booking.booking_date == booking_date, Booking.status == BookingStatus.CONFIRMED.value)
    if exclude_booking_id is not None:
        stmt = stmt.where(Booking.id != exclude_booking_id)
    booked_result = await session.execute(stmt)
    if any(_overlaps(start_time, end_time, item.start_time, item.end_time) for item in booked_result.scalars()):
        raise ConflictError("Slot is not available")


async def get_weekly_booking_limit_minutes(session: AsyncSession) -> int:
    result = await session.execute(select(ClubSetting).where(ClubSetting.key == "weekly_booking_limit_minutes"))
    setting = result.scalar_one_or_none()
    if setting is None:
        return 240  # read-only: a commit here would release the booking advisory lock mid-operation
    try:
        return int(setting.value)
    except ValueError:
        return 240


async def get_daily_booking_limit_minutes(session: AsyncSession) -> int:
    result = await session.execute(select(ClubSetting).where(ClubSetting.key == "daily_booking_limit_minutes"))
    setting = result.scalar_one_or_none()
    if setting is None:
        return 0
    try:
        return int(setting.value)
    except ValueError:
        return 0


async def get_max_future_bookings_per_band(session: AsyncSession) -> int:
    """Cap on a band's not-yet-started bookings (anti-hoarding); 0 means no cap. Read-only like the other getters."""
    result = await session.execute(select(ClubSetting).where(ClubSetting.key == "max_future_bookings_per_band"))
    setting = result.scalar_one_or_none()
    if setting is None:
        return 0
    try:
        return max(0, int(setting.value))
    except ValueError:
        return 0


async def count_band_future_bookings(session: AsyncSession, band_id: int, now: datetime) -> int:
    result = await session.execute(
        select(Booking).where(
            Booking.band_id == band_id,
            Booking.status == BookingStatus.CONFIRMED.value,
            Booking.booking_date >= now.date(),
        )
    )
    return sum(1 for item in result.scalars() if datetime.combine(item.booking_date, item.start_time) > now)


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


async def personal_bookings_are_enabled(session: AsyncSession) -> bool:
    result = await session.execute(select(ClubSetting).where(ClubSetting.key == "personal_bookings_enabled"))
    setting = result.scalar_one_or_none()
    return setting is None or setting.value == "1"


async def list_available_slots(
    session: AsyncSession,
    target_date: date,
    duration_minutes: int = 60,
    band_id: int | None = None,
    privileged: bool = False,
) -> list[dict]:
    if duration_minutes <= 0 or duration_minutes % 30 != 0:
        return []
    now = now_local()
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
    if not privileged and not await is_date_open_for_booking(session, target_date, now):
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


async def get_band_booked_minutes_in_window(
    session: AsyncSession, band_id: int, now: datetime, exclude_booking_id: int | None = None
) -> int:
    window_end = now + timedelta(days=7)
    stmt = select(Booking).where(
        Booking.band_id == band_id,
        Booking.status == BookingStatus.CONFIRMED.value,
        Booking.booking_date >= now.date(),
        Booking.booking_date <= window_end.date(),
    )
    if exclude_booking_id is not None:
        stmt = stmt.where(Booking.id != exclude_booking_id)
    result = await session.execute(stmt)
    total = 0
    for booking in result.scalars():
        booking_start = datetime.combine(booking.booking_date, booking.start_time)
        if booking_start <= now or booking_start > window_end:
            continue
        total += _duration_minutes(booking.start_time, booking.end_time)
    return total


async def get_band_booked_minutes_on_date(
    session: AsyncSession, band_id: int, target_date: date, exclude_booking_id: int | None = None
) -> int:
    stmt = select(Booking).where(
        Booking.band_id == band_id,
        Booking.status == BookingStatus.CONFIRMED.value,
        Booking.booking_date == target_date,
    )
    if exclude_booking_id is not None:
        stmt = stmt.where(Booking.id != exclude_booking_id)
    result = await session.execute(stmt)
    return sum(_duration_minutes(booking.start_time, booking.end_time) for booking in result.scalars())


async def get_booking_limit_status(session: AsyncSession, telegram_id: int, band_id: int, target_date: date | None = None) -> dict:
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_approved(user)
    member_result = await session.execute(select(BandMember).where(BandMember.band_id == band_id, BandMember.user_id == user.id))
    if member_result.scalar_one_or_none() is None:
        raise ForbiddenError("You are not a member of this band")
    now = now_local()
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
    band_id: int | None,
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
    if band_id is None:
        if purpose != "personal":
            raise ConflictError("Personal booking must use personal purpose")
        if not await personal_bookings_are_enabled(session):
            raise ForbiddenError("Personal bookings are disabled")
    else:
        if purpose == "personal":
            raise ConflictError("Band booking cannot use personal purpose")
        member_result = await session.execute(select(BandMember).where(BandMember.band_id == band_id, BandMember.user_id == user.id))
        if member_result.scalar_one_or_none() is None:
            raise ForbiddenError("You are not a member of this band")
    duration = _duration_minutes(start_time, end_time)
    if duration <= 0 or duration % 30 != 0:
        raise ConflictError("Booking duration must be a positive 30-minute step")
    await _lock_bookings(session)
    if band_id is not None:
        max_future = await get_max_future_bookings_per_band(session)
        if max_future > 0 and await count_band_future_bookings(session, band_id, now_local()) >= max_future:
            raise ConflictError(f"Too many future bookings: at most {max_future} per band")
    slots = await list_available_slots(session, booking_date, duration, band_id)
    if not any(slot["start_time"] == start_time and slot["end_time"] == end_time for slot in slots):
        raise ConflictError("Slot is not available")
    if band_id is not None:
        now = now_local()
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
        f"Создал бронь: {'личная' if band_id is None else f'коллектив #{band_id}'}, {booking_date.isoformat()} {start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}, {song_title or 'песня не указана'}",
    )
    await record_change(session, booking, "created", user)
    await commit_and_publish(session)
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
    await _lock_bookings(session)
    slots = await list_available_slots(session, booking_date, duration, privileged=True)
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
        source="staff",
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
    await record_change(session, booking, "created", user)
    await commit_and_publish(session)
    await session.refresh(booking)
    return booking


PRIORITY_HORIZON_DAYS = 30


def _require_priority_rights(user) -> None:
    if not (is_dsvr(user) or is_global_admin(user)):
        raise ForbiddenError("Only DSVR or admins can take over time")


async def _validate_priority_slot(session: AsyncSession, booking_date: date, start_time: time, end_time: time) -> None:
    duration = _duration_minutes(start_time, end_time)
    if duration <= 0 or duration % 30 != 0:
        raise ConflictError("Booking duration must be a positive 30-minute step")
    now = now_local()
    start_dt = datetime.combine(booking_date, start_time)
    if start_dt <= now:
        raise ConflictError("Start time must be in the future")
    if booking_date > (now + timedelta(days=PRIORITY_HORIZON_DAYS)).date():
        raise ConflictError(f"Date must be within the next {PRIORITY_HORIZON_DAYS} days")
    window_start, window_finish = await get_booking_window_minutes(session)
    if _time_to_minutes(start_time) < window_start or _time_to_minutes(end_time) > window_finish:
        raise ConflictError("Booking is outside room working hours")


async def _overlapping_confirmed(session: AsyncSession, booking_date: date, start_time: time, end_time: time) -> list[Booking]:
    result = await session.execute(
        select(Booking)
        .where(Booking.booking_date == booking_date, Booking.status == BookingStatus.CONFIRMED.value)
        .options(*_booking_load_options())
        .order_by(Booking.start_time)
    )
    return [item for item in result.scalars().unique() if _overlaps(start_time, end_time, item.start_time, item.end_time)]


def _preempted_row(booking: Booking) -> dict:
    return {
        "id": booking.id,
        "band_name": _booking_owner_label(booking),
        "booking_date": booking.booking_date.isoformat(),
        "start_time": booking.start_time.isoformat(),
        "end_time": booking.end_time.isoformat(),
        "song_title": booking.song_title,
        **_band_info(booking),
    }


async def preview_priority_booking(
    session: AsyncSession, telegram_id: int, booking_date: date, start_time: time, end_time: time
) -> list[dict]:
    """Bookings that would be cancelled if the caller takes this time; nothing is changed."""
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    _require_priority_rights(user)
    await _validate_priority_slot(session, booking_date, start_time, end_time)
    return [_preempted_row(item) for item in await _overlapping_confirmed(session, booking_date, start_time, end_time)]


async def create_priority_booking(
    session: AsyncSession,
    telegram_id: int,
    booking_date: date,
    start_time: time,
    end_time: time,
    title: str,
) -> dict:
    """DSVR takes the room: overlapping bookings are cancelled (and their people notified), then the slot is theirs."""
    from app.services.audit import log_action

    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    _require_priority_rights(user)
    await _lock_bookings(session)
    await _validate_priority_slot(session, booking_date, start_time, end_time)
    affected = await _overlapping_confirmed(session, booking_date, start_time, end_time)
    cancelled_rows = [_preempted_row(item) for item in affected]
    reason = f"Вытеснено ДСВР: {title}"[:255]
    for item in affected:
        before = snapshot(item)
        item.status = BookingStatus.CANCELLED.value
        item.cancelled_by_id = user.id
        item.cancel_reason = reason
        await record_change(session, item, "preempted", user, before=before, reason=reason)
    booking = Booking(
        band_id=None,
        created_by_id=user.id,
        booking_date=booking_date,
        start_time=start_time,
        end_time=end_time,
        purpose="staff",
        song_title=title,
        source="dsvr",
    )
    session.add(booking)
    await session.flush()
    await log_action(
        session,
        user.id,
        "booking.priority_create",
        "booking",
        booking.id,
        f"ДСВР занял кабинет: {booking_date.isoformat()} {start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}, {title}; отменено броней: {len(affected)}",
    )
    await record_change(session, booking, "created", user, reason=f"Приоритет ДСВР, отменено броней: {len(affected)}")
    await commit_and_publish(session)
    await session.refresh(booking)
    return {"booking": booking, "cancelled": cancelled_rows}


async def delete_booking(session: AsyncSession, telegram_id: int, booking_id: int) -> dict:
    from app.services.audit import log_action

    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    await _lock_bookings(session)
    result = await session.execute(
        select(Booking)
        .where(Booking.id == booking_id)
        .options(selectinload(Booking.band), selectinload(Booking.created_by))
    )
    booking = result.scalar_one_or_none()
    if booking is None:
        raise NotFoundError("Booking not found")
    if not await _can_manage_booking(session, user, booking):
        raise ForbiddenError("You cannot manage this booking")
    if booking.status != BookingStatus.CONFIRMED.value:
        raise ConflictError("Booking is already cancelled")
    _ensure_booking_not_past(booking)
    band_name = _booking_owner_label(booking)
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
    before = snapshot(booking)
    booking.status = BookingStatus.CANCELLED.value
    booking.cancelled_by_id = user.id
    await record_change(session, booking, "cancelled", user, before=before)
    await commit_and_publish(session)
    return deleted


async def update_booking(
    session: AsyncSession,
    telegram_id: int,
    booking_id: int,
    booking_date: date | None = None,
    start_time: time | None = None,
    end_time: time | None = None,
    purpose: str | None = None,
    song_title: str | None = None,
) -> Booking:
    from app.services.audit import log_action

    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    await _lock_bookings(session)
    result = await session.execute(
        select(Booking)
        .where(Booking.id == booking_id)
        .options(selectinload(Booking.band), selectinload(Booking.created_by))
    )
    booking = result.scalar_one_or_none()
    if booking is None:
        raise NotFoundError("Booking not found")
    require_approved(user)
    if not await _can_manage_booking(session, user, booking):
        raise ForbiddenError("You cannot manage this booking")
    if booking.status != BookingStatus.CONFIRMED.value:
        raise ConflictError("Cancelled booking cannot be changed")
    _ensure_booking_not_past(booking)
    before = snapshot(booking)
    if purpose is not None:
        if booking.band_id is not None and purpose in {"staff", "personal"}:
            raise ConflictError("Band booking must use concert or self purpose")
        if booking.band_id is None and purpose in {"concert", "self"}:
            raise ConflictError("Booking without a band must use personal or staff purpose")
    new_date = booking_date or booking.booking_date
    new_start = start_time or booking.start_time
    new_end = end_time or booking.end_time
    if booking_date is not None or start_time is not None or end_time is not None:
        await _ensure_booking_time_available(
            session, new_date, new_start, new_end, booking.id, privileged=is_global_admin(user) or is_dsvr(user)
        )
        if booking.band_id is not None and not (is_global_admin(user) or is_dsvr(user)):
            new_duration = _duration_minutes(new_start, new_end)
            weekly_limit = await get_weekly_booking_limit_minutes(session)
            used = await get_band_booked_minutes_in_window(session, booking.band_id, now_local(), booking.id)
            if used + new_duration > weekly_limit:
                raise ConflictError(f"Weekly booking limit exceeded: {weekly_limit / 60:g}h per band")
            daily_limit = await get_daily_booking_limit_minutes(session)
            if daily_limit > 0:
                used_day = await get_band_booked_minutes_on_date(session, booking.band_id, new_date, booking.id)
                if used_day + new_duration > daily_limit:
                    raise ConflictError(f"Daily booking limit exceeded: {daily_limit / 60:g}h per band")
        booking.booking_date = new_date
        booking.start_time = new_start
        booking.end_time = new_end
    if purpose is not None:
        booking.purpose = purpose
    if song_title is not None:
        booking.song_title = song_title or None
    await log_action(
        session,
        user.id,
        "booking.update",
        "booking",
        booking.id,
        f"Изменил бронь #{booking.id}: {booking.booking_date.isoformat()} {booking.start_time.strftime('%H:%M')}-{booking.end_time.strftime('%H:%M')}",
    )
    await record_change(session, booking, "updated", user, before=before)
    await commit_and_publish(session)
    await session.refresh(booking)
    return booking


async def list_bookings(session: AsyncSession, telegram_id: int | None = None) -> list[Booking]:
    stmt = select(Booking).options(selectinload(Booking.band)).order_by(Booking.booking_date, Booking.start_time)
    if telegram_id is not None:
        user = await get_user_by_telegram_id(session, telegram_id)
        if user is None:
            return []
        stmt = stmt.join(BandMember, BandMember.band_id == Booking.band_id).where(BandMember.user_id == user.id)
    result = await session.execute(stmt)
    return list(result.scalars().unique())


async def list_my_booking_rows(session: AsyncSession, telegram_id: int) -> list[dict]:
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        return []
    stmt = (
        select(Booking)
        .outerjoin(BandMember, BandMember.band_id == Booking.band_id)
        .where(
            Booking.status == BookingStatus.CONFIRMED.value,
            or_(
                Booking.created_by_id == user.id,
                BandMember.user_id == user.id,
            ),
        )
        .options(*_booking_load_options())
        .order_by(Booking.booking_date, Booking.start_time)
    )
    result = await session.execute(stmt)
    rows = []
    now = now_local()
    for booking in result.scalars().unique():
        if datetime.combine(booking.booking_date, booking.end_time) <= now:
            continue
        created_by = booking.created_by
        rows.append(
            {
                "id": booking.id,
                "band_id": booking.band_id,
                "band_name": _booking_owner_label(booking),
                "created_by": f"{created_by.last_name or ''} {created_by.first_name or ''}".strip()
                or created_by.telegram_username
                or str(created_by.telegram_id),
                "booking_date": booking.booking_date,
                "start_time": booking.start_time,
                "end_time": booking.end_time,
                "purpose": booking.purpose,
                "song_title": booking.song_title,
                "status": booking.status,
                "can_manage": await _can_manage_booking(session, user, booking),
                **_band_info(booking),
            }
        )
    return rows


async def list_schedule(session: AsyncSession) -> list[dict]:
    now = now_local()
    window_end = now + timedelta(days=7)
    result = await session.execute(
        select(Booking)
        .where(
            Booking.status == BookingStatus.CONFIRMED.value,
            Booking.booking_date >= now.date(),
            Booking.booking_date <= window_end.date(),
        )
        .options(*_booking_load_options())
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
                "band_name": _booking_owner_label(booking),
                "created_by": f"{created_by.last_name or ''} {created_by.first_name or ''}".strip()
                or created_by.telegram_username
                or str(created_by.telegram_id),
                "booking_date": booking.booking_date,
                "start_time": booking.start_time,
                "end_time": booking.end_time,
                "purpose": booking.purpose,
                "song_title": booking.song_title,
                "status": booking.status,
                **_band_info(booking),
            }
        )
    return rows


async def list_day_closures(session: AsyncSession) -> list[dict]:
    now = now_local()
    days = [now.date() + timedelta(days=offset) for offset in range(7)]
    result = await session.execute(select(AvailableDay).where(AvailableDay.is_enabled.is_(False)))
    closed_by_weekday = {item.day_of_week: item for item in result.scalars()}
    rows = []
    for day in days:
        closed_day = closed_by_weekday.get(day.weekday())
        if closed_day is None:
            continue
        rows.append(
            {
                "date": day,
                "day_of_week": closed_day.day_of_week,
                "occupied_by": closed_day.occupied_by or None,
            }
        )
    return rows
