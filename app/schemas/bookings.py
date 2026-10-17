from datetime import date, time

from pydantic import BaseModel, Field


class BookingCreate(BaseModel):
    telegram_id: int
    band_id: int | None = None
    booking_date: date
    start_time: time
    end_time: time
    purpose: str = Field(default="self", pattern="^(concert|self|personal)$")
    song_title: str | None = Field(default=None, max_length=150)


class WebAppBookingCreate(BaseModel):
    """Mini App booking: who acts comes from the signed initData, never from the body."""

    band_id: int | None = None
    booking_date: date
    start_time: time
    end_time: time
    purpose: str = Field(default="self", pattern="^(concert|self|personal)$")
    song_title: str | None = Field(default=None, max_length=150)


class BookingUpdate(BaseModel):
    telegram_id: int
    booking_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    purpose: str | None = Field(default=None, pattern="^(concert|self|staff|personal)$")
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
    group_title: str | None = None
    responsible_name: str | None = None
    responsible_username: str | None = None
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


class PriorityBookingCreate(StaffBookingCreate):
    pass


class PriorityBookingResult(BaseModel):
    booking: BookingRead
    cancelled: list[dict]


class AvailableSlotRead(BaseModel):
    id: int
    day_of_week: int
    start_time: time
    end_time: time
    is_enabled: bool

    model_config = {"from_attributes": True}
