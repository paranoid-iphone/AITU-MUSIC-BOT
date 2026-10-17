from datetime import time

from pydantic import BaseModel, Field


class WebAppModerate(BaseModel):
    reason: str | None = Field(default=None, max_length=255)


class WebAppRoleChange(BaseModel):
    target: str = Field(min_length=1, max_length=100)
    enabled: bool = True


class WebAppDayUpdate(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    is_enabled: bool
    occupied_by: str | None = Field(default=None, max_length=150)


class WebAppWindowUpdate(BaseModel):
    start_time: time
    end_time: time


class WebAppLimitsUpdate(BaseModel):
    weekly_hours: float | None = Field(default=None, gt=0, le=24)
    daily_hours: float | None = Field(default=None, ge=0, le=24)
    max_future_bookings: int | None = Field(default=None, ge=0, le=100)


class WebAppPersonalUpdate(BaseModel):
    enabled: bool
