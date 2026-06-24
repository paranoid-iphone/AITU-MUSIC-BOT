from datetime import time

from pydantic import BaseModel, Field


class DayUpdate(BaseModel):
    telegram_id: int
    day_of_week: int = Field(ge=0, le=6)
    is_enabled: bool


class SlotCreate(BaseModel):
    telegram_id: int
    day_of_week: int = Field(ge=0, le=6)
    start_time: time
    end_time: time


class SlotToggle(BaseModel):
    telegram_id: int
    is_enabled: bool


class WeeklyBookingLimitUpdate(BaseModel):
    telegram_id: int
    hours: float = Field(gt=0, le=24)


class DailyBookingLimitUpdate(BaseModel):
    telegram_id: int
    hours: float = Field(ge=0, le=24)


class BookingWindowUpdate(BaseModel):
    telegram_id: int
    start_time: time
    end_time: time
