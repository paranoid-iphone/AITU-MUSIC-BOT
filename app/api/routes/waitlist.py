"""Bot-facing waiting-list route (internal token): the "take it" button of a freed-time notification."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_internal_token
from app.db.session import get_session
from app.schemas.bookings import BookingRead
from app.schemas.waitlist import WaitlistTake
from app.services import waitlist

router = APIRouter(dependencies=[Depends(verify_internal_token)])


@router.post("/{entry_id}/take", response_model=BookingRead)
async def take(entry_id: int, payload: WaitlistTake, session: AsyncSession = Depends(get_session)):
    return await waitlist.take(session, payload.telegram_id, entry_id)
