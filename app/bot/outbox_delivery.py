"""Delivery of backend events to Telegram.

An event is moved from `bot:outbox` to `bot:outbox:processing` while it is handled and removed from there only
afterwards, so a bot that dies mid-delivery loses nothing: `recover_processing` puts such events back on start.
Delivery is therefore at least once.
"""
import asyncio
import json
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from redis.asyncio import Redis

from app.bot.menu_refresh import MenuRefresher

OUTBOX_KEY = "bot:outbox"
PROCESSING_KEY = "bot:outbox:processing"
MAX_ATTEMPTS = 5
logger = logging.getLogger(__name__)


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _button_row(*buttons: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data) for text, data in buttons]])


def _message(event: dict) -> tuple[str, InlineKeyboardMarkup | None] | None:
    kind = event.get("type")
    if kind == "notify":
        return event["text"], None
    if kind == "repeat_offer":
        return event["text"], _button_row((event["button"], f"repeat:{event['booking_id']}"))
    if kind == "move_request":
        return event["text"], _button_row(
            (event["accept"], f"move:accept:{event['request_id']}"), (event["decline"], f"move:decline:{event['request_id']}")
        )
    if kind == "waitlist_offer":
        return event["text"], _button_row((event["button"], f"waitlist:take:{event['entry_id']}"))
    return None


async def deliver(bot: Bot, event: dict, refresher: MenuRefresher) -> None:
    """Send one event. A flood limit or a network hiccup is waited out and retried; any other failure is dropped."""
    if event.get("type") == "schedule_changed":
        refresher.mark_dirty()
        return
    message = _message(event)
    if message is None:
        logger.warning("Unknown outbox event: %s", event.get("type"))
        return
    text, markup = message
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            await bot.send_message(event["telegram_id"], text, reply_markup=markup)
            return
        except TelegramRetryAfter as exc:
            logger.warning("Flood limit while notifying %s, retrying in %ss", event["telegram_id"], exc.retry_after)
            delay = exc.retry_after
        except TelegramNetworkError as exc:
            logger.warning("Network error while notifying %s: %s", event["telegram_id"], exc)
            delay = attempt
        except Exception as exc:  # blocked the bot, chat not found, ...: retrying cannot help
            logger.info("Could not deliver %s to %s: %s", event["type"], event["telegram_id"], exc)
            return
        if attempt < MAX_ATTEMPTS:
            await _sleep(delay)
    logger.warning("Gave up delivering %s to %s after %s attempts", event["type"], event["telegram_id"], MAX_ATTEMPTS)


async def recover_processing(redis: Redis) -> int:
    """Put events a previous bot process took but did not finish back at the head of the queue, in order."""
    restored = 0
    while await redis.lmove(PROCESSING_KEY, OUTBOX_KEY, "RIGHT", "LEFT") is not None:
        restored += 1
    return restored


async def process_next(bot: Bot, redis: Redis, refresher: MenuRefresher) -> bool:
    """Take the next event, deliver it and acknowledge it. False when the queue was empty."""
    raw = await redis.blmove(OUTBOX_KEY, PROCESSING_KEY, 1, "LEFT", "RIGHT")
    if raw is None:
        return False
    try:
        await deliver(bot, json.loads(raw), refresher)
    except asyncio.CancelledError:
        raise  # stays in the processing list and is restored on the next start
    except Exception:
        logger.exception("Dropping a malformed outbox event: %s", raw)
    await redis.lrem(PROCESSING_KEY, 1, raw)
    return True
