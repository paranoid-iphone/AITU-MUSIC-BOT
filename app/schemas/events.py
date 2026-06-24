from datetime import date

from pydantic import BaseModel, Field


class EventCreate(BaseModel):
    telegram_id: int
    title: str = Field(min_length=1, max_length=150)
    event_date: date
    location: str | None = Field(default=None, max_length=150)
    submission_deadline: date | None = None


class EventUpdate(BaseModel):
    telegram_id: int
    title: str | None = Field(default=None, min_length=1, max_length=150)
    event_date: date | None = None
    location: str | None = Field(default=None, max_length=150)
    submission_deadline: date | None = None
    status: str | None = Field(default=None, pattern="^(open|closed)$")


class EventRead(BaseModel):
    id: int
    title: str
    event_date: date
    location: str | None
    submission_deadline: date | None
    status: str

    model_config = {"from_attributes": True}


class EventApplicationCreate(BaseModel):
    telegram_id: int
    band_id: int
    song_title: str = Field(min_length=1, max_length=150)
    member_ids: list[int]


class EventApplicationRead(BaseModel):
    id: int
    event_id: int
    band_id: int
    song_title: str | None
    submitted_by_id: int

    model_config = {"from_attributes": True}
