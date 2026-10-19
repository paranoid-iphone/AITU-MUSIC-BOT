"""Waiting list for occupied time ("встать в очередь").

Someone who wants a range that is already booked joins the list for exactly that range, for one of their bands or
as a personal rehearsal. When bookings free time (cancelled, moved, partly preempted) every waiter whose range is
now entirely free is told in the bot and can take it with one tap; whoever taps first books it, the others stay on
the list. Nothing is reserved while a waiter is deciding.
"""
from datetime import date, datetime, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.timezone import now_local
from app.db.models import BandMember, Booking, BookingStatus, WaitlistEntry
from app.services import bookings as booking_service
from app.services.errors import ConflictError, ForbiddenError, NotFoundError
from app.services.outbox import queue_waitlist_offer
from app.services.users import get_user_by_telegram_id, require_approved

MAX_ENTRIES_PER_USER = 5


def _slot(day: date, start: time, end: time) -> str:
    return f"{day.strftime('%d.%m')} {start.strftime('%H:%M')}–{end.strftime('%H:%M')}"


def _label(entry: WaitlistEntry) -> str:
    return entry.band.name if entry.band is not None else "Личная репетиция"


def _load_options():
    return (selectinload(WaitlistEntry.band), selectinload(WaitlistEntry.user))


async def _is_free(session: AsyncSession, day: date, start: time, end: time) -> bool:
    try:
        await booking_service._ensure_booking_time_available(session, day, start, end)
    except ConflictError:
        return False
    return True


async def join(
    session: AsyncSession,
    telegram_id: int,
    band_id: int | None,
    booking_date: date,
    start_time: time,
    end_time: time,
) -> WaitlistEntry:
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_approved(user)
    if band_id is None:
        if not await booking_service.personal_bookings_are_enabled(session):
            raise ForbiddenError("Personal bookings are disabled")
    else:
        member = await session.execute(select(BandMember.id).where(BandMember.band_id == band_id, BandMember.user_id == user.id))
        if member.first() is None:
            raise ForbiddenError("You are not a member of this band")
    await booking_service._lock_bookings(session)
    await booking_service._ensure_booking_range_valid(session, booking_date, start_time, end_time)
    booked = await session.execute(
        select(Booking).where(Booking.booking_date == booking_date, Booking.status == BookingStatus.CONFIRMED.value)
    )
    overlapping = [item for item in booked.scalars() if booking_service._overlaps(start_time, end_time, item.start_time, item.end_time)]
    if not overlapping:
        raise ConflictError("This time is free: book it directly")
    if any(item.purpose == "staff" for item in overlapping):
        raise ConflictError("This time is held by the studio staff")
    own = (await session.execute(select(WaitlistEntry).where(WaitlistEntry.user_id == user.id))).scalars().all()
    if any((e.band_id, e.booking_date, e.start_time, e.end_time) == (band_id, booking_date, start_time, end_time) for e in own):
        raise ConflictError("You are already waiting for this time")
    if len(own) >= MAX_ENTRIES_PER_USER:
        raise ConflictError(f"At most {MAX_ENTRIES_PER_USER} waiting-list entries at a time")
    entry = WaitlistEntry(
        user_id=user.id, band_id=band_id, booking_date=booking_date, start_time=start_time, end_time=end_time
    )
    session.add(entry)
    await session.commit()
    return entry


async def leave(session: AsyncSession, telegram_id: int, entry_id: int) -> None:
    user = await get_user_by_telegram_id(session, telegram_id)
    entry = await session.get(WaitlistEntry, entry_id)
    if user is None or entry is None or entry.user_id != user.id:
        raise NotFoundError("Waiting-list entry not found")
    await session.delete(entry)
    await session.commit()


async def take(session: AsyncSession, telegram_id: int, entry_id: int) -> Booking:
    """Book the entry's range for the waiter. The entry goes away together with a successful booking only."""
    user = await get_user_by_telegram_id(session, telegram_id)
    entry = await session.get(WaitlistEntry, entry_id)
    if user is None or entry is None or entry.user_id != user.id:
        raise NotFoundError("Waiting-list entry not found")
    band_id, day, start, end = entry.band_id, entry.booking_date, entry.start_time, entry.end_time
    await session.delete(entry)
    try:
        # create_booking commits, which also commits the deletion above
        return await booking_service.create_booking(
            session, telegram_id, band_id, day, start, end, "personal" if band_id is None else "self", None
        )
    except Exception:
        await session.rollback()
        raise


async def notify_freed(session: AsyncSession, freed: list[tuple[date, time, time]]) -> int:
    """Offer ranges that were just freed to the waiters they now fit, in queue order. Returns how many were told.

    Call it after the freeing change is applied (flushed) and before the commit; the offers go out with it.
    """
    now = now_local()
    offered: set[int] = set()
    for day, start, end in freed:
        result = await session.execute(
            select(WaitlistEntry)
            .where(WaitlistEntry.booking_date == day)
            .options(*_load_options())
            .order_by(WaitlistEntry.created_at, WaitlistEntry.id)
        )
        for entry in result.scalars():
            if entry.id in offered or not booking_service._overlaps(start, end, entry.start_time, entry.end_time):
                continue
            if datetime.combine(entry.booking_date, entry.start_time) <= now:
                continue
            if not await _is_free(session, entry.booking_date, entry.start_time, entry.end_time):
                continue
            offered.add(entry.id)
            if entry.user.notifications_enabled:
                when = _slot(entry.booking_date, entry.start_time, entry.end_time)
                queue_waitlist_offer(
                    session,
                    entry.user.telegram_id,
                    entry.id,
                    f"Освободилось время {when}, на которое вы стояли в очереди («{_label(entry)}»).\n"
                    f"Кто нажмёт первым, тот и забронирует.",
                    f"🎸 Забрать {entry.start_time.strftime('%H:%M')}",
                )
    return len(offered)


async def cleanup_past(session: AsyncSession) -> int:
    """Drop entries whose time has started. Returns how many."""
    now = now_local()
    result = await session.execute(select(WaitlistEntry).where(WaitlistEntry.booking_date <= now.date()))
    past = [item for item in result.scalars() if datetime.combine(item.booking_date, item.start_time) <= now]
    for item in past:
        await session.delete(item)
    if past:
        await session.commit()
    return len(past)


async def list_for_user(session: AsyncSession, telegram_id: int) -> list[dict]:
    """The user's upcoming entries, with whether the range is entirely free right now."""
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        return []
    await cleanup_past(session)
    result = await session.execute(
        select(WaitlistEntry)
        .where(WaitlistEntry.user_id == user.id)
        .options(*_load_options())
        .order_by(WaitlistEntry.booking_date, WaitlistEntry.start_time, WaitlistEntry.id)
    )
    return [
        {
            "id": entry.id,
            "band_id": entry.band_id,
            "band_name": _label(entry),
            "booking_date": entry.booking_date,
            "start_time": entry.start_time,
            "end_time": entry.end_time,
            "is_free": await _is_free(session, entry.booking_date, entry.start_time, entry.end_time),
        }
        for entry in result.scalars()
    ]
