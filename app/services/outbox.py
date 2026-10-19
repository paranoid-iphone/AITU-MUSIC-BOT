"""Backend -> bot events.

Telegram messages are sent only by the bot process. The backend pushes small JSON events onto a Redis list
(`bot:outbox`) and the bot consumes them. An event is stored in `outbox_events` inside the transaction that causes it,
so a rolled-back change never notifies anyone and a committed one is never lost: after the commit it is pushed to
Redis and stamped `published_at`, and `republish_unpublished` (a scheduler job) pushes the ones that missed Redis.
"""
import json
import logging
from datetime import datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import delete, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import OutboxEvent

logger = logging.getLogger(__name__)

OUTBOX_KEY = "bot:outbox"
REPUBLISH_AFTER = timedelta(seconds=30)  # give commit_and_publish time to publish before the job steps in
KEEP_PUBLISHED = timedelta(days=7)
REPUBLISH_BATCH = 200
_client: Redis | None = None


def _redis() -> Redis:
    global _client
    if _client is None:
        _client = Redis.from_url(settings.redis_url, decode_responses=True)
    return _client


def _pending(session: AsyncSession) -> list[OutboxEvent]:
    """Events queued on this session and not rolled back. A rollback detaches the rows it never committed."""
    events = [item for item in session.info.get("outbox", []) if inspect(item).persistent or inspect(item).pending]
    session.info["outbox"] = events
    return events


def _queue(session: AsyncSession, payload: dict) -> None:
    event = OutboxEvent(payload=payload)
    session.add(event)
    _pending(session).append(event)


def queue_notification(session: AsyncSession, telegram_id: int, text: str) -> None:
    _queue(session, {"type": "notify", "telegram_id": telegram_id, "text": text})


def queue_repeat_offer(session: AsyncSession, telegram_id: int, booking_id: int, text: str, button: str) -> None:
    _queue(
        session,
        {"type": "repeat_offer", "telegram_id": telegram_id, "booking_id": booking_id, "text": text, "button": button},
    )


def queue_move_request(
    session: AsyncSession, telegram_id: int, request_id: int, text: str, accept: str, decline: str
) -> None:
    _queue(
        session,
        {
            "type": "move_request",
            "telegram_id": telegram_id,
            "request_id": request_id,
            "text": text,
            "accept": accept,
            "decline": decline,
        },
    )


def queue_waitlist_offer(session: AsyncSession, telegram_id: int, entry_id: int, text: str, button: str) -> None:
    _queue(
        session,
        {"type": "waitlist_offer", "telegram_id": telegram_id, "entry_id": entry_id, "text": text, "button": button},
    )


def queue_schedule_changed(session: AsyncSession) -> None:
    if not any(item.payload["type"] == "schedule_changed" for item in _pending(session)):
        _queue(session, {"type": "schedule_changed"})


async def publish(events: list[dict]) -> bool:
    """Push events onto the bot's Redis list. False means they did not get there (the caller keeps them unpublished)."""
    if not events:
        return True
    try:
        await _redis().rpush(OUTBOX_KEY, *[json.dumps(item, ensure_ascii=False) for item in events])
    except Exception as exc:  # Redis being down must never fail a booking
        logger.warning("Could not publish %s outbox event(s): %s", len(events), exc)
        return False
    return True


async def commit_and_publish(session: AsyncSession) -> None:
    events = _pending(session)
    if events:
        await session.flush()  # assigns the ids
    sent = [(item.id, item.payload) for item in events]
    session.info.pop("outbox", None)
    await session.commit()
    if not sent:
        return
    if await publish([payload for _, payload in sent]):
        try:
            await session.execute(
                update(OutboxEvent).where(OutboxEvent.id.in_([event_id for event_id, _ in sent])).values(published_at=datetime.utcnow())
            )
            await session.commit()
        except Exception as exc:  # the events are already delivered; the job would only send them a second time
            await session.rollback()
            logger.warning("Could not mark %s outbox event(s) as published: %s", len(sent), exc)
    if any(payload["type"] == "schedule_changed" for _, payload in sent):
        from app.services.sheets_sync import request_sync  # lazy: sheets_sync imports booking code that imports this module

        request_sync()


async def republish_unpublished(session: AsyncSession) -> int:
    """Push committed events that never reached Redis (it was down, or the backend died right after the commit)."""
    total = 0
    cutoff = datetime.utcnow() - REPUBLISH_AFTER
    while True:
        rows = (
            await session.execute(
                select(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None), OutboxEvent.created_at <= cutoff)
                .order_by(OutboxEvent.id)
                .limit(REPUBLISH_BATCH)
            )
        ).scalars().all()
        if not rows or not await publish([row.payload for row in rows]):
            return total
        now = datetime.utcnow()
        for row in rows:
            row.published_at = now
        await session.commit()
        total += len(rows)
        if len(rows) < REPUBLISH_BATCH:
            return total


async def purge_published(session: AsyncSession) -> int:
    """Forget events that were published more than KEEP_PUBLISHED ago."""
    result = await session.execute(delete(OutboxEvent).where(OutboxEvent.published_at < datetime.utcnow() - KEEP_PUBLISHED))
    await session.commit()
    return result.rowcount or 0
