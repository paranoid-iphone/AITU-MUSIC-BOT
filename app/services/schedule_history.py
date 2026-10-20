import re
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Band, BandMember, Booking, ScheduleChange, User
from app.services.errors import ForbiddenError, NotFoundError
from app.services.outbox import queue_notification, queue_schedule_changed
from app.services.permissions import permitted_group_ids
from app.services.search import contains
from app.services.users import get_user_by_telegram_id

# Actions: created | updated | cancelled | preempted | auto_assigned | restored
ACTION_TITLES = {
    "restored": "восстановил(а) бронь из Sheets",
    "created": "создал(а) бронь",
    "updated": "изменил(а) бронь",
    "cancelled": "отменил(а) бронь",
    "preempted": "вытеснил(а) бронь (ДСВР)",
    "auto_assigned": "авто-распределение",
}


def snapshot(booking: Booking) -> dict:
    return {
        "date": booking.booking_date.isoformat(),
        "start": booking.start_time.strftime("%H:%M"),
        "end": booking.end_time.strftime("%H:%M"),
        "purpose": booking.purpose,
        "song_title": booking.song_title,
        "status": booking.status,
    }


def describe_slot(snap: dict | None) -> str:
    if not snap:
        return "-"
    day = date.fromisoformat(snap["date"]).strftime("%d.%m")
    title = f" · {snap['song_title']}" if snap.get("song_title") else ""
    return f"{day} {snap['start']}–{snap['end']}{title}"


# Reasons the backend writes itself. Their text is Russian (the bot and Sheets show it as is); the Mini App gets a
# code or the parts and translates them.
FIXED_REASONS = {
    "Коллектив удалён": "band_deleted",
    "Пользователь заблокирован": "user_banned",
    "Пользователь удалён": "user_deleted",
}
_PREEMPT_REASON = re.compile(r"^Вытеснено (?P<by>админом|ДСВР): (?P<title>.*)$", re.DOTALL)


def reason_parts(reason: str | None) -> dict:
    """Structured form of a known reason, empty for free text."""
    if reason in FIXED_REASONS:
        return {"reason_code": FIXED_REASONS[reason]}
    match = _PREEMPT_REASON.match(reason or "")
    if match:
        return {"reason_code": "preempted", "reason_by": "admin" if match["by"] == "админом" else "dsvr", "reason_title": match["title"]}
    return {}


def owner_kind(item: ScheduleChange) -> str:
    """band | personal | staff, from the booking snapshot (the label itself is stored as Russian text)."""
    snapshot = item.after or item.before or {}
    if item.band_id is None and snapshot.get("purpose") in ("personal", "staff"):
        return snapshot["purpose"]
    return "band"


def person_name(user: User | None) -> str:
    if user is None:
        return "система"
    return f"{user.last_name or ''} {user.first_name or ''}".strip() or user.telegram_username or str(user.telegram_id)


def _owner_label(booking: Booking, band: Band | None) -> str:
    if band is not None:
        return band.name
    return "Личная репетиция" if booking.purpose == "personal" else "Стафф / кабинет"


def _notification_text(action: str, label: str, actor: User | None, before: dict | None, after: dict, reason: str | None) -> str:
    who = person_name(actor)
    if action == "created":
        return f"Новая бронь номера «{label}»: {describe_slot(after)}.\nКто: {who}"
    if action == "updated":
        return f"Расписание номера «{label}» изменено.\nБыло: {describe_slot(before)}\nСтало: {describe_slot(after)}\nКто: {who}"
    if action in {"cancelled", "preempted"}:
        tail = f"\nПричина: {reason}" if reason else ""
        return f"Бронь номера «{label}» отменена: {describe_slot(before or after)}.\nКто: {who}{tail}"
    return f"Расписание номера «{label}»: {describe_slot(after)}.\nКто: {who}" + (f"\n{reason}" if reason else "")


async def record_change(
    session: AsyncSession,
    booking: Booking,
    action: str,
    actor: User | None,
    before: dict | None = None,
    reason: str | None = None,
    notify: bool = True,
) -> ScheduleChange:
    """Write a history row and queue bot events. Call after mutating `booking` (and flushing a new one)."""
    band = await session.get(Band, booking.band_id) if booking.band_id is not None else None
    label = _owner_label(booking, band)
    after = snapshot(booking)
    change = ScheduleChange(
        booking_id=booking.id,
        band_id=booking.band_id,
        group_id=band.group_id if band is not None else None,
        band_name=label,
        booking_date=booking.booking_date,
        actor_id=actor.id if actor is not None else None,
        action=action,
        before=before,
        after=after,
        reason=reason,
    )
    session.add(change)
    queue_schedule_changed(session)
    if notify:
        recipient_ids: list[int] = []
        if band is not None and band.responsible_user_id is not None:
            recipient_ids.append(band.responsible_user_id)
        if action in {"cancelled", "preempted"} and booking.created_by_id not in recipient_ids:
            recipient_ids.append(booking.created_by_id)
        text = _notification_text(action, label, actor, before, after, reason)
        for user_id in recipient_ids:
            if actor is not None and user_id == actor.id:
                continue
            recipient = await session.get(User, user_id)
            if recipient is not None and recipient.notifications_enabled:
                queue_notification(session, recipient.telegram_id, text)
    return change


async def list_changes(
    session: AsyncSession,
    telegram_id: int,
    band_id: int | None = None,
    group_id: int | None = None,
    limit: int = 20,
    before_id: int | None = None,
    search: str | None = None,
    action: str | None = None,
    on_date: date | None = None,
) -> list[dict]:
    """Newest first; pass the last id you got as `before_id` to fetch the next page.

    `search` matches the band label or the name/username of whoever made the change, `on_date` the rehearsal date.
    """
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    stmt = (
        select(ScheduleChange)
        .options(selectinload(ScheduleChange.actor))
        .order_by(ScheduleChange.id.desc())
        .limit(max(1, min(limit, 100)))
    )
    if before_id is not None:
        stmt = stmt.where(ScheduleChange.id < before_id)
    if band_id is not None:
        stmt = stmt.where(ScheduleChange.band_id == band_id)
    if group_id is not None:
        stmt = stmt.where(ScheduleChange.group_id == group_id)
    if action is not None:
        stmt = stmt.where(ScheduleChange.action == action)
    if on_date is not None:
        stmt = stmt.where(ScheduleChange.booking_date == on_date)
    needle = (search or "").strip().lstrip("@")
    if needle:
        stmt = stmt.outerjoin(User, User.id == ScheduleChange.actor_id).where(
            or_(
                contains(ScheduleChange.band_name, needle),
                contains(User.first_name, needle),
                contains(User.last_name, needle),
                contains(User.telegram_username, needle),
            )
        )
    group_ids = await permitted_group_ids(session, user, "schedule.history")
    if group_ids is not None:
        conditions = []
        if group_ids:
            conditions.append(ScheduleChange.group_id.in_(group_ids))
        if band_id is not None:
            member = await session.execute(select(BandMember.id).where(BandMember.band_id == band_id, BandMember.user_id == user.id))
            if member.first() is not None:
                conditions.append(ScheduleChange.band_id == band_id)
        if not conditions:
            raise ForbiddenError("You cannot view this history")
        stmt = stmt.where(or_(*conditions))
    result = await session.execute(stmt)
    return [
        {
            "id": item.id,
            "booking_id": item.booking_id,
            "band_id": item.band_id,
            "band_name": item.band_name,
            "group_id": item.group_id,
            "booking_date": item.booking_date.isoformat() if item.booking_date is not None else None,
            "action": item.action,
            "actor_name": person_name(item.actor),
            "actor_is_system": item.actor is None,
            "owner_kind": owner_kind(item),
            "before": item.before,
            "after": item.after,
            "reason": item.reason,
            **reason_parts(item.reason),
            "created_at": item.created_at.isoformat(),
        }
        for item in result.scalars()
    ]
