from datetime import date, time

from pydantic import BaseModel, Field


class BookingCreate(BaseModel):
    telegram_id: int
    band_id: int
    booking_date: date
    start_time: time
    end_time: time
    purpose: str = Field(default="self", pattern="^(concert|self)$")
    song_title: str | None = Field(default=None, max_length=150)


class BookingRead(BaseModel):
    id: int
    band_id: int | None
    booking_date: date
    start_time: time
    end_time: time
    purpose: str
    song_title: str | None
    status: str

    model_config = {"from_attributes": True}


class ScheduleBookingRead(BaseModel):
    id: int
    band_name: str
    created_by: str
    booking_date: date
    start_time: time
    end_time: time
    purpose: str
    song_title: str | None
    status: str


class StaffBookingCreate(BaseModel):
    telegram_id: int
    booking_date: date
    start_time: time
    end_time: time
    title: str = Field(min_length=1, max_length=150)


class AvailableSlotRead(BaseModel):
    id: int
    day_of_week: int
    start_time: time
    end_time: time
    is_enabled: bool

    model_config = {"from_attributes": True}
