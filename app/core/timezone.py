from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.config import settings


def now_local() -> datetime:
    """Current wall-clock time in the club timezone, as a naive datetime.

    Bookings are stored as naive local dates/times, so comparisons must use the same basis.
    """
    return datetime.now(ZoneInfo(settings.timezone)).replace(tzinfo=None)


def format_moment(moment: datetime) -> str:
    """Club-local moment as shown to people in messages."""
    return moment.strftime("%d.%m %H:%M")
