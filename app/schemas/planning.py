from datetime import time

from pydantic import BaseModel, Field


class WindowIn(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    start_time: time
    end_time: time


class PlanningRequestCreate(BaseModel):
    telegram_id: int
    band_id: int
    duration_minutes: int = Field(ge=30, le=480)
    purpose: str = Field(default="self", pattern="^(concert|self)$")
    song_title: str | None = Field(default=None, max_length=150)
    windows: list[WindowIn] = Field(min_length=1, max_length=5)


class AllocateRequest(BaseModel):
    telegram_id: int
    dry_run: bool = False
