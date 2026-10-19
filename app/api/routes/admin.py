from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_internal_token
from app.db.session import get_session
from app.services import admin, audit

router = APIRouter(dependencies=[Depends(verify_internal_token)])


@router.get("/settings/registration-notify")
async def registration_notify_mode(session: AsyncSession = Depends(get_session)):
    return {"mode": await admin.get_registration_notify_mode(session)}


@router.get("/actions/history")
async def action_history(telegram_id: int, limit: int = 30, session: AsyncSession = Depends(get_session)):
    return await audit.list_actions(session, telegram_id, limit)
