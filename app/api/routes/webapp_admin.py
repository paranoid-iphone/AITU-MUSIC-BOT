"""Mini App admin API: registration requests, users and roles. Authenticated by signed `initData`, not the internal token.

Permission rules live in `services/users.py` and `services/groups.py`; these routes only check who may open the
screen at all and translate the services' results into JSON.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.webapp_auth import current_webapp_user, fresh_webapp_user
from app.db.models import Group, User, UserRole, UserStatus
from app.db.session import get_session
from app.schemas.webapp_admin import WebAppModerate, WebAppRoleChange
from app.services import groups as group_service
from app.services import outbox
from app.services import users as user_service
from app.services.errors import ForbiddenError
from app.services.permissions import administered_group_ids, is_global_admin

router = APIRouter(prefix="/webapp/api/admin", tags=["webapp-admin"])


async def _require_staff(session: AsyncSession, user: User) -> User:
    if not is_global_admin(user) and not await administered_group_ids(session, user):
        raise ForbiddenError("Admin access required")
    return user


async def admin_reader(user: User = Depends(current_webapp_user), session: AsyncSession = Depends(get_session)) -> User:
    return await _require_staff(session, user)


async def admin_writer(user: User = Depends(fresh_webapp_user), session: AsyncSession = Depends(get_session)) -> User:
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
    status: str | None = None,
    q: str | None = None,
    admin: User = Depends(admin_reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    users, counts = await user_service.search_users(session, admin.telegram_id, status, q)
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
