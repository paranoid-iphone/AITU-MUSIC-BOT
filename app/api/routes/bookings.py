from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_internal_token
from app.db.session import get_session
from app.schemas.bookings import BookingRead, BookingRepeat, ScheduleBookingRead
from app.services import bookings, repeat_offers

router = APIRouter(dependencies=[Depends(verify_internal_token)])


@router.get("/mine", response_model=list[ScheduleBookingRead])
async def my_bookings(telegram_id: int, session: AsyncSession = Depends(get_session)):
    return await bookings.list_my_booking_rows(session, telegram_id)


@router.get("/schedule", response_model=list[ScheduleBookingRead])
async def schedule(session: AsyncSession = Depends(get_session)):
    return await bookings.list_schedule(session)


@router.get("/day-closures")
async def day_closures(session: AsyncSession = Depends(get_session)):
    rows = await bookings.list_day_closures(session)
    return [{**item, "date": item["date"].isoformat()} for item in rows]


@router.post("/{booking_id}/repeat", response_model=BookingRead)
async def repeat_booking(booking_id: int, payload: BookingRepeat, session: AsyncSession = Depends(get_session)):
    return await repeat_offers.repeat_booking(session, payload.telegram_id, booking_id)
