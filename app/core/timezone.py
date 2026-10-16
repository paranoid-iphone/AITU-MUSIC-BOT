from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.config import settings


def now_local() -> datetime:
    """Current wall-clock time in the club timezone, as a naive datetime.

    Bookings are stored as naive local dates/times, so comparisons must use the same basis.
    """
    return datetime.now(ZoneInfo(settings.timezone)).replace(tzinfo=None)
