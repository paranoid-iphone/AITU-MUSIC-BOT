"""Bot-facing move-request routes (internal token): the buttons of a "please move" notification."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_internal_token
from app.db.session import get_session
from app.schemas.move_requests import MoveAnswer, MoveRequestRead
from app.services import move_requests

router = APIRouter(dependencies=[Depends(verify_internal_token)])


@router.post("/{request_id}/accept", response_model=MoveRequestRead)
async def accept(request_id: int, payload: MoveAnswer, session: AsyncSession = Depends(get_session)):
    return await move_requests.accept(session, payload.telegram_id, request_id)


@router.post("/{request_id}/decline", response_model=MoveRequestRead)
async def decline(request_id: int, payload: MoveAnswer, session: AsyncSession = Depends(get_session)):
    return await move_requests.decline(session, payload.telegram_id, request_id)
