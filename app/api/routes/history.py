from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_internal_token
from app.db.session import get_session
from app.services import schedule_history

router = APIRouter(dependencies=[Depends(verify_internal_token)])


@router.get("")
async def schedule_changes(
    telegram_id: int,
    band_id: int | None = None,
    group_id: int | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
):
    return await schedule_history.list_changes(session, telegram_id, band_id, group_id, limit)
