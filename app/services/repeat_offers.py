"""Offer to repeat a rehearsal one week later.

The booking window slides, so the slot exactly a week after a booking only becomes bookable once that booking
has started. A background job finds bookings that have just started and, if the same slot a week later is free,
sends the responsible person a one-tap offer (`repeat_booking` is what the button calls).
"""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_local
from app.db.models import Booking, BookingStatus
from app.services import bookings as booking_service
from app.services.errors import ConflictError, NotFoundError
from app.services.errors import ForbiddenError
from app.services.outbox import commit_and_publish, queue_repeat_offer
from app.services.users import get_user_by_telegram_id, require_approved

REPEAT_INTERVAL = timedelta(days=7)
# A booking is only offered while it has started recently; this also bounds the work after a backend outage.
REPEAT_OFFER_LOOKBACK = timedelta(hours=2)


def _start_dt(booking: Booking) -> datetime:
    return datetime.combine(booking.booking_date, booking.start_time)


def _recipient(booking: Booking):
    """The band's responsible person; the creator for personal bookings and bands without one."""
    if booking.band is not None and booking.band.responsible is not None:
        return booking.band.responsible
    return booking.created_by


async def _can_repeat(session: AsyncSession, booking: Booking, now: datetime) -> bool:
    target_date = booking.booking_date + REPEAT_INTERVAL
    duration = booking_service._duration_minutes(booking.start_time, booking.end_time)
    slots = await booking_service.list_available_slots(session, target_date, duration, booking.band_id)
    if not any(slot["start_time"] == booking.start_time and slot["end_time"] == booking.end_time for slot in slots):
        return False
    if booking.band_id is not None:
        max_future = await booking_service.get_max_future_bookings_per_band(session)
        if max_future > 0 and await booking_service.count_band_future_bookings(session, booking.band_id, now) >= max_future:
            return False
    return True


async def offer_repeats(session: AsyncSession) -> int:
    """Queue a repeat offer for every just-started booking whose slot a week later is free. Returns how many."""
    now = now_local()
    since = now - REPEAT_OFFER_LOOKBACK
    result = await session.execute(
        select(Booking)
        .where(
            Booking.status == BookingStatus.CONFIRMED.value,
            Booking.purpose != "staff",  # staff and DSVR bookings are not rehearsals
            Booking.repeat_offered_at.is_(None),
            Booking.booking_date >= since.date(),
            Booking.booking_date <= now.date(),
        )
        .options(*booking_service._booking_load_options())
        .order_by(Booking.booking_date, Booking.start_time)
    )
    offered = 0
    for booking in result.scalars().unique():
        if not since < _start_dt(booking) <= now:
            continue
        recipient = _recipient(booking)
        if recipient is None or not recipient.notifications_enabled:
            continue
        if not await _can_repeat(session, booking, now):
            continue
        target_date = booking.booking_date + REPEAT_INTERVAL
        label = booking.band.name if booking.band is not None else "Личная репетиция"
        day = target_date.strftime("%d.%m")
        start, end = booking.start_time.strftime("%H:%M"), booking.end_time.strftime("%H:%M")
        queue_repeat_offer(
            session,
            recipient.telegram_id,
            booking.id,
            f"Репетиция «{label}» началась. Повторить через неделю — {day} {start}–{end}? Слот свободен.",
            f"🔁 Забронировать {day} {start}",
        )
        booking.repeat_offered_at = now
        offered += 1
    if offered:
        await commit_and_publish(session)
    return offered


async def repeat_booking(session: AsyncSession, telegram_id: int, booking_id: int) -> Booking:
    """Book the same slot, band, purpose and song one week after `booking_id`, as `telegram_id`."""
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_approved(user)
    result = await session.execute(select(Booking).where(Booking.id == booking_id))
    booking = result.scalar_one_or_none()
    if booking is None:
        raise NotFoundError("Booking not found")
    if booking.status != BookingStatus.CONFIRMED.value or booking.purpose == "staff":
        raise ConflictError("This booking cannot be repeated")
    if booking.band_id is None and booking.created_by_id != user.id:
        raise ForbiddenError("You cannot repeat this booking")
    return await booking_service.create_booking(
        session,
        telegram_id,
        booking.band_id,
        booking.booking_date + REPEAT_INTERVAL,
        booking.start_time,
        booking.end_time,
        booking.purpose,
        booking.song_title,
    )
