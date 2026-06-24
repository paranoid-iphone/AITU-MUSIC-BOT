from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_internal_token
from app.db.session import get_session
from app.schemas.bookings import AvailableSlotRead, BookingCreate, BookingRead, ScheduleBookingRead, StaffBookingCreate
from app.services import bookings

router = APIRouter(dependencies=[Depends(verify_internal_token)])


@router.get("", response_model=list[BookingRead])
async def list_bookings(telegram_id: int | None = None, session: AsyncSession = Depends(get_session)):
    return await bookings.list_bookings(session, telegram_id)


@router.get("/schedule", response_model=list[ScheduleBookingRead])
async def schedule(session: AsyncSession = Depends(get_session)):
    return await bookings.list_schedule(session)


@router.get("/available-slots", response_model=list[AvailableSlotRead])
async def available_slots(
    target_date: date,
    duration_minutes: int = Query(default=60, ge=30, le=1440),
    band_id: int | None = None,
    session: AsyncSession = Depends(get_session),
):
    return await bookings.list_available_slots(session, target_date, duration_minutes, band_id)


@router.get("/limit-status")
async def limit_status(
    telegram_id: int,
    band_id: int,
    target_date: date | None = None,
    session: AsyncSession = Depends(get_session),
):
    return await bookings.get_booking_limit_status(session, telegram_id, band_id, target_date)


@router.post("", response_model=BookingRead)
async def create_booking(payload: BookingCreate, session: AsyncSession = Depends(get_session)):
    return await bookings.create_booking(
        session,
        payload.telegram_id,
        payload.band_id,
        payload.booking_date,
        payload.start_time,
        payload.end_time,
        payload.purpose,
        payload.song_title,
    )


@router.post("/staff", response_model=BookingRead)
async def create_staff_booking(payload: StaffBookingCreate, session: AsyncSession = Depends(get_session)):
    return await bookings.create_staff_booking(
        session,
        payload.telegram_id,
        payload.booking_date,
        payload.start_time,
        payload.end_time,
        payload.title,
    )


@router.delete("/{booking_id}")
async def delete_booking(booking_id: int, telegram_id: int, session: AsyncSession = Depends(get_session)):
    return await bookings.delete_booking(session, telegram_id, booking_id)
