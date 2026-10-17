from datetime import date, time

from pydantic import BaseModel, Field


class WebAppMoveRequestCreate(BaseModel):
    """Ask to move a booking; who asks comes from the signed initData."""

    booking_id: int
    proposed_date: date
    start_time: time
    end_time: time
    message: str | None = Field(default=None, max_length=200)


class MoveAnswer(BaseModel):
    telegram_id: int


class MoveRequestRead(BaseModel):
    id: int
    booking_id: int
    status: str

    model_config = {"from_attributes": True}
