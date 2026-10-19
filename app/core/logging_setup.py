import logging

from app.core.config import settings

NOISY_LOGGERS = ("httpx", "apscheduler")


def setup_logging() -> None:
    """Make the `app.*` loggers visible: root handler, LOG_LEVEL, and quiet per-request / per-job libraries."""
    level = logging.getLevelName(settings.log_level.strip().upper())
    logging.basicConfig(
        level=level if isinstance(level, int) else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
