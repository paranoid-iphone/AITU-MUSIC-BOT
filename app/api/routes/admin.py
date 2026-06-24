from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_internal_token
from app.db.session import get_session
from app.schemas.admin import BookingWindowUpdate, DailyBookingLimitUpdate, DayUpdate, SlotCreate, SlotToggle, WeeklyBookingLimitUpdate
from app.schemas.bookings import AvailableSlotRead
from app.services import admin, audit

router = APIRouter(dependencies=[Depends(verify_internal_token)])


@router.put("/days", response_model=dict)
async def update_day(payload: DayUpdate, session: AsyncSession = Depends(get_session)):
    day = await admin.update_available_day(session, payload.telegram_id, payload.day_of_week, payload.is_enabled)
    return {"day_of_week": day.day_of_week, "is_enabled": day.is_enabled}


@router.post("/slots", response_model=AvailableSlotRead)
async def create_slot(payload: SlotCreate, session: AsyncSession = Depends(get_session)):
    return await admin.create_slot(session, payload.telegram_id, payload.day_of_week, payload.start_time, payload.end_time)


@router.patch("/slots/{slot_id}", response_model=AvailableSlotRead)
async def toggle_slot(slot_id: int, payload: SlotToggle, session: AsyncSession = Depends(get_session)):
    return await admin.toggle_slot(session, payload.telegram_id, slot_id, payload.is_enabled)


@router.get("/settings/weekly-booking-limit")
async def get_weekly_booking_limit(telegram_id: int, session: AsyncSession = Depends(get_session)):
    return await admin.get_weekly_booking_limit(session, telegram_id)


@router.put("/settings/weekly-booking-limit")
async def update_weekly_booking_limit(payload: WeeklyBookingLimitUpdate, session: AsyncSession = Depends(get_session)):
    return await admin.update_weekly_booking_limit(session, payload.telegram_id, payload.hours)


@router.get("/settings/daily-booking-limit")
async def get_daily_booking_limit(telegram_id: int, session: AsyncSession = Depends(get_session)):
    return await admin.get_daily_booking_limit(session, telegram_id)


@router.put("/settings/daily-booking-limit")
async def update_daily_booking_limit(payload: DailyBookingLimitUpdate, session: AsyncSession = Depends(get_session)):
    return await admin.update_daily_booking_limit(session, payload.telegram_id, payload.hours)


@router.get("/settings/booking-window")
async def get_booking_window(telegram_id: int, session: AsyncSession = Depends(get_session)):
    return await admin.get_booking_window(session, telegram_id)


@router.put("/settings/booking-window")
async def update_booking_window(payload: BookingWindowUpdate, session: AsyncSession = Depends(get_session)):
    return await admin.update_booking_window(session, payload.telegram_id, payload.start_time, payload.end_time)


@router.get("/bookings/history")
async def booking_history(telegram_id: int, session: AsyncSession = Depends(get_session)):
    return await admin.booking_history(session, telegram_id)


@router.get("/actions/history")
async def action_history(telegram_id: int, limit: int = 30, session: AsyncSession = Depends(get_session)):
    return await audit.list_recent_actions(session, telegram_id, limit)
