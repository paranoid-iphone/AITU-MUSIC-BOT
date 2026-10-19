"""Ask the owners of a booking to move it ("попросить подвинуться").

Someone who needs a slot that is already booked proposes a free slot of the same length for the booking. The
band's responsible person is notified; anyone who may manage the booking (or the responsible person) can accept,
which moves the booking through `update_booking` so history, notifications and the Sheets backup behave as for
any other edit. Nothing is booked for the requester: they are told the slot is free and book it themselves.
"""
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.timezone import now_local
from app.db.models import Band, Booking, BookingStatus, MoveRequest, MoveRequestStatus, User
from app.services import bookings as booking_service
from app.services.audit import log_action
from app.services.errors import ConflictError, ForbiddenError, NotFoundError
from app.services.outbox import commit_and_publish, queue_move_request, queue_notification
from app.services.permissions import can_manage_schedule, is_band_member
from app.services.schedule_history import person_name
from app.services.users import get_user_by_telegram_id, require_approved

REQUEST_TTL = timedelta(hours=24)
MAX_PENDING_PER_REQUESTER = 3
PENDING = MoveRequestStatus.PENDING.value


def _load_options():
    return (
        selectinload(MoveRequest.booking).selectinload(Booking.band).selectinload(Band.responsible),
        selectinload(MoveRequest.booking).selectinload(Booking.created_by),
        selectinload(MoveRequest.requester),
    )


def _start_dt(booking: Booking) -> datetime:
    return datetime.combine(booking.booking_date, booking.start_time)


def _slot(day: date, start: time, end: time) -> str:
    return f"{day.strftime('%d.%m')} {start.strftime('%H:%M')}–{end.strftime('%H:%M')}"


def _booking_slot(booking: Booking) -> str:
    return _slot(booking.booking_date, booking.start_time, booking.end_time)


def _proposed_slot(request: MoveRequest) -> str:
    return _slot(request.proposed_date, request.proposed_start, request.proposed_end)


def _notify(session: AsyncSession, user: User | None, text: str) -> None:
    if user is not None and user.notifications_enabled:
        queue_notification(session, user.telegram_id, text)


async def _can_answer(session: AsyncSession, user: User, booking: Booking) -> bool:
    if not (user.status == "approved" or user.role == "admin"):
        return False
    if await can_manage_schedule(session, user, booking):
        return True
    return booking.band is not None and booking.band.responsible_user_id == user.id


async def _get_request(session: AsyncSession, request_id: int) -> MoveRequest:
    result = await session.execute(
        select(MoveRequest).where(MoveRequest.id == request_id).options(*_load_options()).execution_options(populate_existing=True)
    )
    request = result.scalar_one_or_none()
    if request is None:
        raise NotFoundError("Move request not found")
    return request


def _is_stale(request: MoveRequest, now: datetime) -> bool:
    booking = request.booking
    return (
        request.status == PENDING
        and (request.expires_at <= now or booking.status != BookingStatus.CONFIRMED.value or _start_dt(booking) <= now)
    )


def _expire(session: AsyncSession, request: MoveRequest, now: datetime) -> None:
    request.status = MoveRequestStatus.EXPIRED.value
    request.resolved_at = now
    _notify(
        session,
        request.requester,
        f"Просьба перенести бронь «{booking_service.booking_owner_label(request.booking)}» "
        f"({_booking_slot(request.booking)}) осталась без ответа или потеряла смысл — она закрыта.",
    )


async def create_request(
    session: AsyncSession,
    telegram_id: int,
    booking_id: int,
    proposed_date: date,
    proposed_start: time,
    proposed_end: time,
    message: str | None = None,
) -> MoveRequest:
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_approved(user)
    await booking_service._lock_bookings(session)
    result = await session.execute(
        select(Booking).where(Booking.id == booking_id).options(*booking_service._booking_load_options())
    )
    booking = result.scalar_one_or_none()
    if booking is None:
        raise NotFoundError("Booking not found")
    now = now_local()
    if booking.status != BookingStatus.CONFIRMED.value or booking.purpose == "staff":
        raise ConflictError("This booking cannot be moved")
    if _start_dt(booking) <= now:
        raise ConflictError("This booking has already started")
    if await can_manage_schedule(session, user, booking) or (
        booking.band_id is not None and await is_band_member(session, user, booking.band_id)
    ):
        raise ForbiddenError("You can change your own booking yourself")
    duration = booking_service._duration_minutes(booking.start_time, booking.end_time)
    if booking_service._duration_minutes(proposed_start, proposed_end) != duration:
        raise ConflictError("The proposed slot must be as long as the booking")
    if (proposed_date, proposed_start, proposed_end) == (booking.booking_date, booking.start_time, booking.end_time):
        raise ConflictError("The proposed slot is the current one")
    await booking_service._ensure_booking_time_available(
        session, proposed_date, proposed_start, proposed_end, exclude_booking_id=booking.id
    )
    pending = (
        await session.execute(
            select(MoveRequest).where(
                MoveRequest.requester_id == user.id, MoveRequest.status == PENDING, MoveRequest.expires_at > now
            )
        )
    ).scalars().all()
    if any(item.booking_id == booking.id for item in pending):
        raise ConflictError("You have already asked to move this booking")
    if len(pending) >= MAX_PENDING_PER_REQUESTER:
        raise ConflictError(f"At most {MAX_PENDING_PER_REQUESTER} open move requests at a time")

    request = MoveRequest(
        booking_id=booking.id,
        requester_id=user.id,
        proposed_date=proposed_date,
        proposed_start=proposed_start,
        proposed_end=proposed_end,
        message=(message or "").strip() or None,
        expires_at=min(now + REQUEST_TTL, _start_dt(booking)),
    )
    session.add(request)
    await session.flush()
    await log_action(
        session, user.id, "move_request.create", "move_request", request.id,
        f"Попросил перенести бронь #{booking.id}: {_booking_slot(booking)} → {_proposed_slot(request)}",
    )
    contact = booking_service.booking_contact(booking)
    if contact is not None and contact.notifications_enabled and contact.id != user.id:
        note = f"\nСообщение: {request.message}" if request.message else ""
        queue_move_request(
            session,
            contact.telegram_id,
            request.id,
            f"{person_name(user)} просит подвинуть репетицию «{booking_service.booking_owner_label(booking)}»:\n"
            f"сейчас {_booking_slot(booking)}, предлагается {_proposed_slot(request)}.{note}\n"
            f"Ответить нужно до {request.expires_at.strftime('%d.%m %H:%M')}.",
            "✅ Согласен",
            "❌ Отказать",
        )
    await commit_and_publish(session)
    request.requester = user
    request.booking = booking
    return request


async def _answerable(session: AsyncSession, telegram_id: int, request_id: int) -> tuple[User, MoveRequest]:
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    await booking_service._lock_bookings(session)
    request = await _get_request(session, request_id)
    if not await _can_answer(session, user, request.booking):
        raise ForbiddenError("You cannot answer this request")
    if request.status != PENDING:
        raise ConflictError("This request is already closed")
    now = now_local()
    if _is_stale(request, now):
        _expire(session, request, now)
        await commit_and_publish(session)
        raise ConflictError("This request has expired")
    return user, request


async def accept(session: AsyncSession, telegram_id: int, request_id: int) -> MoveRequest:
    user, request = await _answerable(session, telegram_id, request_id)
    booking = request.booking
    old_slot = _booking_slot(booking)
    # Raises ConflictError (and changes nothing) if the proposed slot has been taken in the meantime.
    await booking_service.update_booking(
        session,
        user.telegram_id,
        booking.id,
        request.proposed_date,
        request.proposed_start,
        request.proposed_end,
        authorized=True,
        commit=False,
    )
    now = now_local()
    request.status = MoveRequestStatus.ACCEPTED.value
    request.resolved_at = now
    request.resolved_by_id = user.id
    label = booking_service.booking_owner_label(booking)
    _notify(
        session,
        request.requester,
        f"«{label}» согласились подвинуться: было {old_slot}, теперь {_booking_slot(booking)}.\n"
        f"Слот {old_slot} свободен — забронируйте его в расписании, пока не заняли.",
    )
    others = await session.execute(
        select(MoveRequest)
        .where(MoveRequest.booking_id == booking.id, MoveRequest.status == PENDING, MoveRequest.id != request.id)
        .options(*_load_options())
    )
    for other in others.scalars():
        _expire(session, other, now)
    await log_action(
        session, user.id, "move_request.accept", "move_request", request.id,
        f"Согласился перенести бронь #{booking.id}: {old_slot} → {_booking_slot(booking)}",
    )
    await commit_and_publish(session)
    return request


async def decline(session: AsyncSession, telegram_id: int, request_id: int) -> MoveRequest:
    user, request = await _answerable(session, telegram_id, request_id)
    request.status = MoveRequestStatus.DECLINED.value
    request.resolved_at = now_local()
    request.resolved_by_id = user.id
    _notify(
        session,
        request.requester,
        f"По «{booking_service.booking_owner_label(request.booking)}» ({_booking_slot(request.booking)}) "
        f"вам отказали: бронь остаётся на месте.",
    )
    await log_action(session, user.id, "move_request.decline", "move_request", request.id, f"Отказал в переносе брони #{request.booking_id}")
    await commit_and_publish(session)
    return request


async def cancel(session: AsyncSession, telegram_id: int, request_id: int) -> MoveRequest:
    """The requester withdraws their own open request."""
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    request = await _get_request(session, request_id)
    if request.requester_id != user.id:
        raise ForbiddenError("This is not your request")
    if request.status != PENDING:
        raise ConflictError("This request is already closed")
    request.status = MoveRequestStatus.CANCELLED.value
    request.resolved_at = now_local()
    request.resolved_by_id = user.id
    await commit_and_publish(session)
    return request


async def expire_stale(session: AsyncSession) -> int:
    """Close open requests that ran out of time or whose booking was cancelled or has started. Returns how many."""
    now = now_local()
    result = await session.execute(select(MoveRequest).where(MoveRequest.status == PENDING).options(*_load_options()))
    stale = [request for request in result.scalars() if _is_stale(request, now)]
    for request in stale:
        _expire(session, request, now)
    if stale:
        await commit_and_publish(session)
    return len(stale)


def _row(request: MoveRequest) -> dict:
    booking, requester = request.booking, request.requester
    return {
        "id": request.id,
        "booking_id": booking.id,
        "band_name": booking_service.booking_owner_label(booking),
        **booking_service.booking_owner_info(booking),
        "song_title": booking.song_title,
        "booking_date": booking.booking_date,
        "start_time": booking.start_time,
        "end_time": booking.end_time,
        "proposed_date": request.proposed_date,
        "proposed_start": request.proposed_start,
        "proposed_end": request.proposed_end,
        "message": request.message,
        "requester_name": person_name(requester),
        "requester_username": requester.telegram_username,
        "expires_at": request.expires_at,
    }


async def list_for_user(session: AsyncSession, telegram_id: int) -> dict:
    """Open requests the user can answer (`incoming`) and the ones they made (`outgoing`)."""
    user = await get_user_by_telegram_id(session, telegram_id)
    empty = {"incoming": [], "outgoing": []}
    if user is None:
        return empty
    now = now_local()
    result = await session.execute(
        select(MoveRequest)
        .where(MoveRequest.status == PENDING, MoveRequest.expires_at > now)
        .options(*_load_options())
        .order_by(MoveRequest.expires_at)
    )
    incoming, outgoing = [], []
    for request in result.scalars():
        if _is_stale(request, now):
            continue
        if request.requester_id == user.id:
            outgoing.append(_row(request))
        elif await _can_answer(session, user, request.booking):
            incoming.append(_row(request))
    return {"incoming": incoming, "outgoing": outgoing}
