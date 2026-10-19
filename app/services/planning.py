from datetime import date, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.timezone import now_local, utcnow
from app.db.models import (
    AvailabilityRequest,
    AvailabilityWindow,
    AvailableDay,
    Band,
    Booking,
    BookingStatus,
    RequestStatus,
    SchedulingWeek,
    User,
    WeekStatus,
)
from app.services import allocator
from app.services.bookings import (
    _lock_bookings,
    get_booking_window_minutes,
    get_daily_booking_limit_minutes,
    get_weekly_booking_limit_minutes,
)
from app.services.errors import ConflictError, ForbiddenError, NotFoundError
from app.services.outbox import commit_and_publish, queue_notification, queue_schedule_changed
from app.services.permissions import can_manage_band, is_global_admin
from app.services.schedule_history import record_change
from app.services.users import get_user_by_telegram_id, require_approved
from app.services.weeks import allocation_deadline, collecting_week, get_week, next_week_start, week_start_of

MAX_WINDOWS = 5
MAX_DURATION_MINUTES = 8 * 60
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _from_minutes(value: int) -> time:
    return time(value // 60, value % 60)


async def _can_submit_for_band(session: AsyncSession, user: User, band: Band) -> bool:
    if band.responsible_user_id == user.id:
        return True
    return await can_manage_band(session, user, band)


def _request_to_dict(request: AvailabilityRequest) -> dict:
    return {
        "id": request.id,
        "week_start": request.week_start.isoformat(),
        "band_id": request.band_id,
        "band_name": request.band.name,
        "duration_minutes": request.duration_minutes,
        "purpose": request.purpose,
        "song_title": request.song_title,
        "status": request.status,
        "booking_id": request.booking_id,
        "windows": [
            {
                "priority": window.priority,
                "day_of_week": window.day_of_week,
                "start_time": window.start_time.strftime("%H:%M"),
                "end_time": window.end_time.strftime("%H:%M"),
            }
            for window in request.windows
        ],
    }


async def get_week_overview(session: AsyncSession, telegram_id: int | None = None) -> dict:
    now = now_local()
    collecting = await collecting_week(session, now)
    week = collecting or next_week_start(now.date())
    row = await get_week(session, week)
    published = row is not None and row.status == WeekStatus.PUBLISHED.value
    is_open = collecting is not None
    counts = {"pending": 0, "assigned": 0, "unassigned": 0}
    result = await session.execute(select(AvailabilityRequest.status).where(AvailabilityRequest.week_start == week))
    for (status,) in result.all():
        counts[status] = counts.get(status, 0) + 1
    return {
        "week_start": week.isoformat(),
        "week_end": (week + timedelta(days=6)).isoformat(),
        "deadline": allocation_deadline(week).isoformat(),
        "is_open": is_open,
        "published": published,
        "requests": counts,
    }


async def submit_request(
    session: AsyncSession,
    telegram_id: int,
    band_id: int,
    duration_minutes: int,
    purpose: str,
    song_title: str | None,
    windows: list[dict],
) -> dict:
    from app.services.audit import log_action

    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_approved(user)
    band = await session.get(Band, band_id)
    if band is None:
        raise NotFoundError("Band not found")
    if not await _can_submit_for_band(session, user, band):
        raise ForbiddenError("Only the band leader or responsible person can submit a request")
    week = await collecting_week(session, now_local())
    if week is None:
        raise ConflictError("Requests for the next week are closed")
    if purpose not in {"concert", "self"}:
        raise ConflictError("Purpose must be concert or self")
    if duration_minutes < 30 or duration_minutes > MAX_DURATION_MINUTES or duration_minutes % 30:
        raise ConflictError("Duration must be a multiple of 30 minutes, up to 8 hours")
    if not 1 <= len(windows) <= MAX_WINDOWS:
        raise ConflictError(f"Provide from 1 to {MAX_WINDOWS} time windows")

    room_start, room_end = await get_booking_window_minutes(session)
    enabled_days = {
        item.day_of_week
        for item in (await session.execute(select(AvailableDay).where(AvailableDay.is_enabled.is_(True)))).scalars()
    }
    cleaned: list[AvailabilityWindow] = []
    for index, window in enumerate(windows, start=1):
        day, start, end = window["day_of_week"], window["start_time"], window["end_time"]
        if not 0 <= day <= 6:
            raise ConflictError("Invalid weekday")
        if day not in enabled_days:
            raise ConflictError(f"{WEEKDAYS[day]}: room is not available on this weekday")
        if _minutes(start) % 30 or _minutes(end) % 30 or _minutes(start) >= _minutes(end):
            raise ConflictError(f"Priority {index}: use 30-minute steps and start before end")
        if _minutes(start) < room_start or _minutes(end) > room_end:
            raise ConflictError(f"Priority {index}: outside room hours")
        if _minutes(end) - _minutes(start) < duration_minutes:
            raise ConflictError(f"Priority {index}: the window is shorter than the rehearsal")
        cleaned.append(AvailabilityWindow(priority=index, day_of_week=day, start_time=start, end_time=end))

    existing = await session.execute(
        select(AvailabilityRequest.duration_minutes).where(
            AvailabilityRequest.week_start == week,
            AvailabilityRequest.band_id == band_id,
            AvailabilityRequest.status == RequestStatus.PENDING.value,
        )
    )
    requested = sum(row[0] for row in existing.all()) + duration_minutes
    weekly_limit = await get_weekly_booking_limit_minutes(session)
    if requested > weekly_limit:
        raise ConflictError(f"Weekly limit exceeded: {weekly_limit / 60:g}h per band")

    request = AvailabilityRequest(
        week_start=week,
        band_id=band_id,
        submitted_by_id=user.id,
        duration_minutes=duration_minutes,
        purpose=purpose,
        song_title=song_title,
        windows=cleaned,
    )
    session.add(request)
    await session.flush()
    await log_action(
        session,
        user.id,
        "planning.request.create",
        "availability_request",
        request.id,
        f"Заявка на неделю {week.isoformat()}: {band.name}, {duration_minutes // 60}ч {duration_minutes % 60}м, окон: {len(cleaned)}",
    )
    await session.commit()
    created = await _load_request(session, request.id)
    return _request_to_dict(created)


async def _load_request(session: AsyncSession, request_id: int) -> AvailabilityRequest:
    result = await session.execute(
        select(AvailabilityRequest)
        .where(AvailabilityRequest.id == request_id)
        .options(selectinload(AvailabilityRequest.windows), selectinload(AvailabilityRequest.band))
        .execution_options(populate_existing=True)
    )
    request = result.scalar_one_or_none()
    if request is None:
        raise NotFoundError("Request not found")
    return request


async def list_requests(session: AsyncSession, telegram_id: int, week_start: date | None = None) -> list[dict]:
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    now = now_local()
    week = week_start or (await collecting_week(session, now)) or next_week_start(now.date())
    result = await session.execute(
        select(AvailabilityRequest)
        .where(AvailabilityRequest.week_start == week)
        .options(selectinload(AvailabilityRequest.windows), selectinload(AvailabilityRequest.band))
        .order_by(AvailabilityRequest.created_at, AvailabilityRequest.id)
    )
    rows = []
    allowed: dict[int, bool] = {}
    for request in result.scalars().unique():
        if request.band_id not in allowed:
            allowed[request.band_id] = await _can_submit_for_band(session, user, request.band)
        if allowed[request.band_id]:
            rows.append(_request_to_dict(request))
    return rows


async def delete_request(session: AsyncSession, telegram_id: int, request_id: int) -> dict:
    from app.services.audit import log_action

    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    request = await _load_request(session, request_id)
    if not await _can_submit_for_band(session, user, request.band):
        raise ForbiddenError("You cannot manage this request")
    if request.status != RequestStatus.PENDING.value:
        raise ConflictError("The request was already processed")
    deleted = _request_to_dict(request)
    await log_action(
        session, user.id, "planning.request.delete", "availability_request", request.id, f"Удалил заявку на неделю: {request.band.name}"
    )
    await session.delete(request)
    await session.commit()
    return deleted


def _format_assignment(week: date, assignment: allocator.Assignment) -> str:
    day = week + timedelta(days=assignment.day)
    return f"{WEEKDAYS[assignment.day]} {day.strftime('%d.%m')} {_from_minutes(assignment.start).strftime('%H:%M')}–{_from_minutes(assignment.end).strftime('%H:%M')}"


async def run_weekly_allocation(
    session: AsyncSession,
    actor_telegram_id: int | None,
    week_start: date | None = None,
    dry_run: bool = False,
) -> dict:
    """Allocate the room for `week_start` (default: the next week) and publish it.

    actor_telegram_id None means the scheduler. With dry_run nothing is written.
    """
    from app.services.audit import log_action

    actor = None
    if actor_telegram_id is not None:
        actor = await get_user_by_telegram_id(session, actor_telegram_id)
        if actor is None:
            raise NotFoundError("User not found")
        if not is_global_admin(actor):
            raise ForbiddenError("Admin access required")

    now = now_local()
    week = week_start or next_week_start(now.date())
    if week.weekday() != 0:
        raise ConflictError("Week must start on Monday")
    if week + timedelta(days=6) < now.date():
        raise ConflictError("This week is already over")

    if not dry_run:
        await _lock_bookings(session)
    row = await get_week(session, week)
    if row is not None and row.status == WeekStatus.PUBLISHED.value:
        raise ConflictError("The schedule for this week was already published")

    requests = list(
        (
            await session.execute(
                select(AvailabilityRequest)
                .where(AvailabilityRequest.week_start == week, AvailabilityRequest.status == RequestStatus.PENDING.value)
                .options(
                    selectinload(AvailabilityRequest.windows),
                    selectinload(AvailabilityRequest.band),
                    selectinload(AvailabilityRequest.submitted_by),
                )
                .order_by(AvailabilityRequest.created_at, AvailabilityRequest.id)
            )
        )
        .scalars()
        .unique()
    )

    room_start, room_end = await get_booking_window_minutes(session)
    daily_limit = await get_daily_booking_limit_minutes(session)
    closed_days = {
        item.day_of_week
        for item in (await session.execute(select(AvailableDay).where(AvailableDay.is_enabled.is_(False)))).scalars()
    }
    busy: dict[int, list[tuple[int, int]]] = {day: [] for day in range(7)}
    band_day_minutes: dict[tuple[int, int], int] = {}
    existing = await session.execute(
        select(Booking).where(
            Booking.status == BookingStatus.CONFIRMED.value,
            Booking.booking_date >= week,
            Booking.booking_date <= week + timedelta(days=6),
        )
    )
    for booking in existing.scalars():
        day = (booking.booking_date - week).days
        busy[day].append((_minutes(booking.start_time), _minutes(booking.end_time)))
        if booking.band_id is not None:
            key = (booking.band_id, day)
            band_day_minutes[key] = band_day_minutes.get(key, 0) + _minutes(booking.end_time) - _minutes(booking.start_time)
    # A week that has already started (late run after downtime): the past is not available.
    for day in range(7):
        current = week + timedelta(days=day)
        if current < now.date():
            closed_days.add(day)
        elif current == now.date():
            rounded = ((now.hour * 60 + now.minute) // 30 + 1) * 30
            busy[day].append((0, rounded))

    allocator_requests = [
        allocator.Request(
            id=request.id,
            band_id=request.band_id,
            duration=request.duration_minutes,
            order=index,
            windows=tuple(
                allocator.Window(w.priority, w.day_of_week, _minutes(w.start_time), _minutes(w.end_time)) for w in request.windows
            ),
        )
        for index, request in enumerate(requests)
    ]
    outcome = allocator.allocate(
        allocator_requests, busy, room_start, room_end, closed_days=closed_days, daily_limit=daily_limit, band_day_minutes=band_day_minutes
    )
    by_id = {request.id: request for request in requests}

    assigned_rows = [
        {
            "request_id": a.request_id,
            "band_name": by_id[a.request_id].band.name,
            "date": (week + timedelta(days=a.day)).isoformat(),
            "label": _format_assignment(week, a),
            "start_time": _from_minutes(a.start).strftime("%H:%M"),
            "end_time": _from_minutes(a.end).strftime("%H:%M"),
            "priority": a.priority,
            "song_title": by_id[a.request_id].song_title,
        }
        for a in outcome.assignments
    ]
    unassigned_rows = [
        {
            "request_id": rid,
            "band_name": by_id[rid].band.name,
            "duration_minutes": by_id[rid].duration_minutes,
        }
        for rid in outcome.unassigned
    ]
    summary = {"week_start": week.isoformat(), "published": not dry_run, "assigned": assigned_rows, "unassigned": unassigned_rows}
    if dry_run:
        return summary

    for assignment in outcome.assignments:
        request = by_id[assignment.request_id]
        booking = Booking(
            band_id=request.band_id,
            created_by_id=request.submitted_by_id,
            booking_date=week + timedelta(days=assignment.day),
            start_time=_from_minutes(assignment.start),
            end_time=_from_minutes(assignment.end),
            purpose=request.purpose,
            song_title=request.song_title,
            source="auto",
            request_id=request.id,
        )
        session.add(booking)
        await session.flush()
        request.status = RequestStatus.ASSIGNED.value
        request.booking_id = booking.id
        await record_change(
            session,
            booking,
            "auto_assigned",
            actor,
            reason=f"Авто-распределение: приоритет {assignment.priority} из {len(request.windows)}",
            notify=False,
        )
        await _notify_request_people(
            session,
            request,
            f"Расписание на неделю составлено: номер «{request.band.name}» — {_format_assignment(week, assignment)} "
            f"(приоритет {assignment.priority}).",
        )
    for request_id in outcome.unassigned:
        request = by_id[request_id]
        request.status = RequestStatus.UNASSIGNED.value
        await _notify_request_people(
            session,
            request,
            f"Не удалось подобрать время для номера «{request.band.name}» "
            f"({request.duration_minutes // 60}ч {request.duration_minutes % 60:02d}м): все указанные окна заняты. "
            "Свободные слоты можно забронировать вручную в разделе «Репетиции».",
        )

    if row is None:
        row = SchedulingWeek(week_start=week)
        session.add(row)
    row.status = WeekStatus.PUBLISHED.value
    row.generated_at = utcnow()
    queue_schedule_changed(session)
    await log_action(
        session,
        actor.id if actor else None,
        "planning.allocate",
        "scheduling_week",
        week.isoformat(),
        f"Авто-распределение недели {week.isoformat()}: назначено {len(assigned_rows)}, без места {len(unassigned_rows)}",
    )
    await commit_and_publish(session)
    return summary


async def _notify_request_people(session: AsyncSession, request: AvailabilityRequest, text: str) -> None:
    """Tell the band's responsible person and whoever submitted the request (once each)."""
    recipient_ids = []
    if request.band.responsible_user_id is not None:
        recipient_ids.append(request.band.responsible_user_id)
    if request.submitted_by_id not in recipient_ids:
        recipient_ids.append(request.submitted_by_id)
    for user_id in recipient_ids:
        user = await session.get(User, user_id)
        if user is not None and user.notifications_enabled:
            queue_notification(session, user.telegram_id, text)


async def catch_up_allocations(session: AsyncSession) -> list[date]:
    """After downtime: allocate any week whose request deadline already passed without being published."""
    now = now_local()
    done: list[date] = []
    for week in (week_start_of(now.date()), next_week_start(now.date())):
        row = await get_week(session, week)
        if row is not None and row.status == WeekStatus.PUBLISHED.value:
            continue
        if now < allocation_deadline(week):
            continue
        pending = await session.execute(
            select(AvailabilityRequest.id).where(
                AvailabilityRequest.week_start == week, AvailabilityRequest.status == RequestStatus.PENDING.value
            )
        )
        if pending.first() is None and week == week_start_of(now.date()):
            continue  # nothing was requested for the week that is already running: leave it alone
        await run_weekly_allocation(session, None, week)
        done.append(week)
    return done
