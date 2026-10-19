from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_internal_token
from app.db.session import get_session
from app.schemas.users import UserModerationUpdate, UserRead, UserSettingsUpdate, UserTelegramUpsert
from app.services import outbox, users

router = APIRouter(dependencies=[Depends(verify_internal_token)])


@router.post("/telegram", response_model=UserRead)
async def upsert_user(payload: UserTelegramUpsert, session: AsyncSession = Depends(get_session)):
    return await users.upsert_telegram_user(
        session,
        payload.telegram_id,
        payload.telegram_username,
        payload.telegram_full_name,
    )


@router.get("", response_model=list[UserRead])
async def list_users(admin_telegram_id: int, status: str | None = None, session: AsyncSession = Depends(get_session)):
    return await users.list_users_by_status(session, admin_telegram_id, status)


@router.post("/approve", response_model=UserRead)
async def approve_user(payload: UserModerationUpdate, session: AsyncSession = Depends(get_session)):
    user = await users.approve_user(session, payload.admin_telegram_id, payload.target)
    await outbox.commit_and_publish(session)
    return user


@router.post("/reject", response_model=UserRead)
async def reject_user(payload: UserModerationUpdate, session: AsyncSession = Depends(get_session)):
    user = await users.reject_user(session, payload.admin_telegram_id, payload.target, payload.reason)
    await outbox.commit_and_publish(session)
    return user


@router.get("/{telegram_id}", response_model=UserRead)
async def get_user(telegram_id: int, requester_telegram_id: int, session: AsyncSession = Depends(get_session)):
    return await users.get_user_for_requester(session, telegram_id, requester_telegram_id)


@router.patch("/{telegram_id}/settings", response_model=UserRead)
async def update_settings(telegram_id: int, payload: UserSettingsUpdate, session: AsyncSession = Depends(get_session)):
    data = payload.model_dump(exclude_unset=True)
    return await users.update_profile(session, telegram_id, **data)


@router.post("/{telegram_id}/resubmit", response_model=UserRead)
async def resubmit_registration(telegram_id: int, session: AsyncSession = Depends(get_session)):
    return await users.resubmit_registration(session, telegram_id)
