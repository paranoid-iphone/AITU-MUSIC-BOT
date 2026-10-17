from datetime import date, time

from pydantic import BaseModel


class WebAppWaitlistJoin(BaseModel):
    """Wait for an occupied range; who waits comes from the signed initData."""

    band_id: int | None = None
    booking_date: date
    start_time: time
    end_time: time


class WaitlistTake(BaseModel):
    telegram_id: int
