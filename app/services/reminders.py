"""Remind people of their rehearsals: the evening before (from EVENING_AT) and again SOON_BEFORE the start.

A background job (`main.run_rehearsal_reminders`, every few minutes) calls `send_reminders`. A band rehearsal reminds
every member of the band and its responsible person, a personal one its author; staff / DSVR bookings are not
rehearsals. Only approved people with both `notifications_enabled` and `rehearsal_reminders_enabled` get them.

Each booking is reminded once per kind (`reminded_evening_at`, `reminded_soon_at`); `bookings.update_booking` clears
both when the date or time changes. A reminder whose moment has passed (the backend was down) is not sent late: the
evening one only goes out on the day before, the "soon" one only before the start.
"""
from datetime import datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.timezone import now_local
from app.db.models import Band, BandMember, Booking, BookingStatus, User, UserStatus
from app.services.outbox import commit_and_publish, queue_rehearsal_reminder

EVENING_AT = time(20, 0)
SOON_BEFORE = timedelta(hours=2)
TURN_OFF_BUTTON = "🔕 Не напоминать"


def _created_local(booking: Booking) -> datetime:
    """`created_at` is naive UTC; bookings and the clock are club-local."""
    return booking.created_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone)).replace(tzinfo=None)


def _recipients(booking: Booking) -> list[User]:
    people: dict[int, User] = {}
    if booking.band is not None:
        for member in booking.band.members:
            people.setdefault(member.user.id, member.user)
        if booking.band.responsible is not None:
            people.setdefault(booking.band.responsible.id, booking.band.responsible)
    else:
        people[booking.created_by.id] = booking.created_by
    return [
        person
        for person in people.values()
        if person.status == UserStatus.APPROVED.value and person.notifications_enabled and person.rehearsal_reminders_enabled
    ]


def _text(booking: Booking, day_word: str) -> str:
    start, end = booking.start_time.strftime("%H:%M"), booking.end_time.strftime("%H:%M")
    what = f"репетиция «{booking.band.name}»" if booking.band is not None else "у вас личная репетиция"
    return f"🔔 {day_word} в {start} {what} (до {end})."


async def send_reminders(session: AsyncSession) -> int:
    """Queue the reminders that are due now. Returns how many bookings were reminded."""
    now = now_local()
    tomorrow = now.date() + timedelta(days=1)
    result = await session.execute(
        select(Booking)
        .where(
            Booking.status == BookingStatus.CONFIRMED.value,
            Booking.purpose != "staff",
            Booking.booking_date >= now.date(),
            Booking.booking_date <= tomorrow,
            or_(Booking.reminded_evening_at.is_(None), Booking.reminded_soon_at.is_(None)),
        )
        .options(
            selectinload(Booking.band).selectinload(Band.members).selectinload(BandMember.user),
            selectinload(Booking.band).selectinload(Band.responsible),
            selectinload(Booking.created_by),
        )
        .order_by(Booking.booking_date, Booking.start_time)
        .execution_options(populate_existing=True)  # a session that saw the band before must not serve stale members
    )
    reminded = 0
    for booking in result.scalars().unique():
        start = datetime.combine(booking.booking_date, booking.start_time)
        if booking.reminded_soon_at is None and start - SOON_BEFORE <= now < start:
            # Settles the evening one too: a rehearsal at 00:30 must not get "tomorrow" after "today".
            booking.reminded_soon_at = now
            booking.reminded_evening_at = booking.reminded_evening_at or now
            if _created_local(booking) > start - SOON_BEFORE:
                continue  # booked within the last two hours: they know
            text = _text(booking, "Сегодня")
        elif booking.reminded_evening_at is None and booking.booking_date == tomorrow and now.time() >= EVENING_AT:
            booking.reminded_evening_at = now
            text = _text(booking, "Завтра")
        else:
            continue
        for person in _recipients(booking):
            queue_rehearsal_reminder(session, person.telegram_id, text, TURN_OFF_BUTTON)
        reminded += 1
    await commit_and_publish(session)
    return reminded
