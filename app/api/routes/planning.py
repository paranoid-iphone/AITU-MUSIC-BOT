from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_internal_token
from app.db.session import get_session
from app.schemas.planning import AllocateRequest, PlanningRequestCreate
from app.services import planning

router = APIRouter(dependencies=[Depends(verify_internal_token)])


@router.get("/week")
async def week_overview(session: AsyncSession = Depends(get_session)):
    return await planning.get_week_overview(session)


@router.get("/requests")
async def list_requests(telegram_id: int, session: AsyncSession = Depends(get_session)):
    return await planning.list_requests(session, telegram_id)


@router.post("/requests")
async def create_request(payload: PlanningRequestCreate, session: AsyncSession = Depends(get_session)):
    return await planning.submit_request(
        session,
        payload.telegram_id,
        payload.band_id,
        payload.duration_minutes,
        payload.purpose,
        payload.song_title,
        [window.model_dump() for window in payload.windows],
    )


@router.delete("/requests/{request_id}")
async def delete_request(request_id: int, telegram_id: int, session: AsyncSession = Depends(get_session)):
    return await planning.delete_request(session, telegram_id, request_id)


@router.post("/allocate")
async def allocate(payload: AllocateRequest, session: AsyncSession = Depends(get_session)):
    return await planning.run_weekly_allocation(session, payload.telegram_id, dry_run=payload.dry_run)
