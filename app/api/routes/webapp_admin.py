"""Mini App admin API: registration requests, users and roles. Authenticated by signed `initData`, not the internal token.

Permission rules live in `services/users.py` and `services/groups.py`; these routes only check who may open the
screen at all and translate the services' results into JSON.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.webapp_auth import current_webapp_user, fresh_webapp_user
from app.core.config import settings
from app.db.models import Group, User, UserRole, UserStatus
from app.db.session import get_session
from app.schemas.webapp_admin import (
    WebAppDayUpdate,
    WebAppLimitsUpdate,
    WebAppModerate,
    WebAppPersonalUpdate,
    WebAppRoleChange,
    WebAppWindowUpdate,
)
from app.services import admin as admin_service
from app.services import groups as group_service
from app.services import outbox
from app.services import users as user_service
from app.services.errors import ForbiddenError
from app.services import schedule_history
from app.services.permissions import administered_group_ids, is_dsvr, is_global_admin

router = APIRouter(prefix="/webapp/api/admin", tags=["webapp-admin"])


async def _require_staff(session: AsyncSession, user: User) -> User:
    if not is_global_admin(user) and not await administered_group_ids(session, user):
        raise ForbiddenError("Admin access required")
    return user


async def admin_reader(user: User = Depends(current_webapp_user), session: AsyncSession = Depends(get_session)) -> User:
    return await _require_staff(session, user)


async def admin_writer(user: User = Depends(fresh_webapp_user), session: AsyncSession = Depends(get_session)) -> User:
    return await _require_staff(session, user)


async def history_reader(user: User = Depends(current_webapp_user), session: AsyncSession = Depends(get_session)) -> User:
    """The history is also open to DSVR, who have no other admin screen; `list_changes` scopes the rows."""
    if is_dsvr(user):
        return user
    return await _require_staff(session, user)


async def main_admin_reader(user: User = Depends(current_webapp_user)) -> User:
    user_service.require_admin(user)
    return user


async def main_admin_writer(user: User = Depends(fresh_webapp_user)) -> User:
    user_service.require_admin(user)
    return user


def _group_payload(group: Group | None) -> dict | None:
    return {"id": group.id, "slug": group.slug, "title": group.title} if group else None


async def person(session: AsyncSession, user: User) -> dict:
    group = await session.get(Group, user.group_id) if user.group_id else None
    return {
        "telegram_id": user.telegram_id,
        "telegram_username": user.telegram_username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "study_group": user.study_group,
        "group": _group_payload(group),
        "role": user.role,
        "status": user.status,
        "moderation_reason": user.moderation_reason,
        "created_at": user.created_at.isoformat(timespec="seconds"),
    }


async def _people(session: AsyncSession, users: list[User]) -> list[dict]:
    return [await person(session, user) for user in users]


@router.get("/users")
async def list_users(
    status: UserStatus | None = None,
    q: str | None = None,
    admin: User = Depends(admin_reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    users, counts = await user_service.search_users(session, admin.telegram_id, status.value if status else None, q)
    return {"users": await _people(session, users), "counts": counts}


@router.get("/users/{telegram_id}")
async def get_user(telegram_id: int, admin: User = Depends(admin_reader), session: AsyncSession = Depends(get_session)) -> dict:
    return await person(session, await user_service.get_user_for_requester(session, telegram_id, admin.telegram_id))


@router.post("/users/{telegram_id}/approve")
async def approve(telegram_id: int, admin: User = Depends(admin_writer), session: AsyncSession = Depends(get_session)) -> dict:
    target = await user_service.approve_user(session, admin.telegram_id, str(telegram_id))
    await outbox.commit_and_publish(session)
    return await person(session, target)


@router.post("/users/{telegram_id}/reject")
async def reject(
    telegram_id: int,
    payload: WebAppModerate,
    admin: User = Depends(admin_writer),
    session: AsyncSession = Depends(get_session),
) -> dict:
    target = await user_service.reject_user(session, admin.telegram_id, str(telegram_id), payload.reason)
    await outbox.commit_and_publish(session)
    return await person(session, target)


@router.post("/users/{telegram_id}/ban")
async def ban(
    telegram_id: int,
    payload: WebAppModerate,
    admin: User = Depends(admin_writer),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await person(session, await user_service.ban_user(session, admin.telegram_id, str(telegram_id), payload.reason))


@router.post("/users/{telegram_id}/delete")
async def delete(
    telegram_id: int,
    payload: WebAppModerate,
    admin: User = Depends(admin_writer),
    session: AsyncSession = Depends(get_session),
) -> dict:
    target = await user_service.soft_delete_user(session, admin.telegram_id, str(telegram_id), payload.reason)
    return await person(session, target)


@router.get("/staff")
async def staff(admin: User = Depends(main_admin_reader), session: AsyncSession = Depends(get_session)) -> dict:
    admins = await user_service.list_admins(session, admin.telegram_id)
    dsvr_rows = await session.execute(
        select(User)
        .where(User.role == UserRole.DSVR.value, User.status == UserStatus.APPROVED.value)
        .order_by(User.created_at.desc())
    )
    group_admins = []
    for group in await group_service.list_groups(session):
        members = await group_service.list_group_admins(session, admin.telegram_id, group.id)
        group_admins.append({"group": _group_payload(group), "admins": await _people(session, members)})
    return {
        "admins": await _people(session, admins),
        "dsvr": await _people(session, list(dsvr_rows.scalars())),
        "group_admins": group_admins,
    }


@router.post("/staff/admin")
async def set_admin(
    payload: WebAppRoleChange, admin: User = Depends(main_admin_writer), session: AsyncSession = Depends(get_session)
) -> dict:
    change = user_service.promote_to_admin if payload.enabled else user_service.demote_admin
    target = await change(session, admin.telegram_id, payload.target)
    await outbox.commit_and_publish(session)
    return await person(session, target)


@router.post("/staff/dsvr")
async def set_dsvr(
    payload: WebAppRoleChange, admin: User = Depends(main_admin_writer), session: AsyncSession = Depends(get_session)
) -> dict:
    target = await user_service.set_dsvr_role(session, admin.telegram_id, payload.target, payload.enabled)
    await outbox.commit_and_publish(session)
    return await person(session, target)


@router.post("/groups/{group_id}/admins")
async def set_group_admin(
    group_id: int,
    payload: WebAppRoleChange,
    admin: User = Depends(main_admin_writer),
    session: AsyncSession = Depends(get_session),
) -> dict:
    change = group_service.add_group_admin if payload.enabled else group_service.remove_group_admin
    target = await change(session, admin.telegram_id, group_id, payload.target)
    await outbox.commit_and_publish(session)
    return await person(session, target)


@router.get("/settings")
async def get_settings(admin: User = Depends(main_admin_reader), session: AsyncSession = Depends(get_session)) -> dict:
    return await admin_service.get_settings(session, admin.telegram_id)


@router.put("/settings/days")
async def set_day(
    payload: WebAppDayUpdate, admin: User = Depends(main_admin_writer), session: AsyncSession = Depends(get_session)
) -> dict:
    day = await admin_service.update_available_day(
        session, admin.telegram_id, payload.day_of_week, payload.is_enabled, payload.occupied_by
    )
    await outbox.commit_and_publish(session)
    return {"day_of_week": day.day_of_week, "is_enabled": day.is_enabled, "occupied_by": day.occupied_by}


@router.put("/settings/window")
async def set_window(
    payload: WebAppWindowUpdate, admin: User = Depends(main_admin_writer), session: AsyncSession = Depends(get_session)
) -> dict:
    window = await admin_service.update_booking_window(session, admin.telegram_id, payload.start_time, payload.end_time)
    await outbox.commit_and_publish(session)
    return {"start_time": window["start_time"], "end_time": window["end_time"]}


@router.put("/settings/limits")
async def set_limits(
    payload: WebAppLimitsUpdate, admin: User = Depends(main_admin_writer), session: AsyncSession = Depends(get_session)
) -> dict:
    """Only the limits present in the body change."""
    await admin_service.update_limits(
        session, admin.telegram_id, payload.weekly_hours, payload.daily_hours, payload.max_future_bookings
    )
    settings = await admin_service.get_settings(session, admin.telegram_id)
    return {key: settings[key] for key in ("weekly_hours", "daily_hours", "max_future_bookings")}


@router.put("/settings/personal")
async def set_personal(
    payload: WebAppPersonalUpdate, admin: User = Depends(main_admin_writer), session: AsyncSession = Depends(get_session)
) -> dict:
    result = await admin_service.update_personal_bookings_enabled(session, admin.telegram_id, payload.enabled)
    return {"personal_enabled": result["personal_bookings_enabled"]}


def _club_local(iso_utc: str) -> str:
    """`ScheduleChange.created_at` is naive UTC; the Mini App shows club time."""
    local = datetime.fromisoformat(iso_utc).replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone))
    return local.strftime("%Y-%m-%dT%H:%M")


@router.get("/history")
async def history(
    group_id: int | None = None,
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=20, ge=1, le=50),
    viewer: User = Depends(history_reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Schedule changes the viewer may see, newest first; `next_before_id` is the cursor for the next page."""
    rows = await schedule_history.list_changes(
        session, viewer.telegram_id, group_id=group_id, limit=limit + 1, before_id=before_id
    )
    page = rows[:limit]
    if is_global_admin(viewer) or is_dsvr(viewer):
        groups = (await session.execute(select(Group).order_by(Group.title))).scalars().all()
    else:
        ids = await administered_group_ids(session, viewer)
        groups = (await session.execute(select(Group).where(Group.id.in_(ids)).order_by(Group.title))).scalars().all()
    return {
        "items": [{**row, "created_local": _club_local(row["created_at"])} for row in page],
        "next_before_id": page[-1]["id"] if len(rows) > limit else None,
        "groups": [_group_payload(group) for group in groups],
    }
