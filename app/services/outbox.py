"""Backend -> bot events.

Telegram messages are sent only by the bot process. The backend pushes small JSON events onto a Redis list
(`bot:outbox`) and the bot consumes them. Events are queued on the session during a transaction and published
only after it commits, so a rolled-back change never notifies anyone.
"""
import json
import logging

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

logger = logging.getLogger(__name__)

OUTBOX_KEY = "bot:outbox"
_client: Redis | None = None


def _redis() -> Redis:
    global _client
    if _client is None:
        _client = Redis.from_url(settings.redis_url, decode_responses=True)
    return _client


def queue_notification(session: AsyncSession, telegram_id: int, text: str) -> None:
    session.info.setdefault("outbox", []).append({"type": "notify", "telegram_id": telegram_id, "text": text})


def queue_schedule_changed(session: AsyncSession) -> None:
    events = session.info.setdefault("outbox", [])
    if not any(item["type"] == "schedule_changed" for item in events):
        events.append({"type": "schedule_changed"})


async def publish(events: list[dict]) -> None:
    if not events:
        return
    try:
        await _redis().rpush(OUTBOX_KEY, *[json.dumps(item, ensure_ascii=False) for item in events])
    except Exception as exc:  # Redis being down must never fail a booking
        logger.warning("Could not publish %s outbox event(s): %s", len(events), exc)


async def commit_and_publish(session: AsyncSession) -> None:
    await session.commit()
    events = session.info.pop("outbox", [])
    await publish(events)
    if any(item["type"] == "schedule_changed" for item in events):
        from app.services.sheets_sync import request_sync  # lazy: sheets_sync imports booking code that imports this module

        request_sync()
