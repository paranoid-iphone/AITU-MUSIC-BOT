import asyncio
import json
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from redis.asyncio import Redis

from app.bot.errors import on_error
from app.bot.handlers import refresh_all_schedule_menus, router
from app.bot.handlers_planning import router as planning_router
from app.bot.handlers_move import router as move_router
from app.bot.handlers_repeat import router as repeat_router
from app.bot.handlers_waitlist import router as waitlist_router
from app.bot.menu_button import setup_menu_button
from app.core.config import settings


OUTBOX_KEY = "bot:outbox"
logger = logging.getLogger(__name__)


async def outbox_worker(bot: Bot) -> None:
    """Deliver events queued by the backend: personal notifications and schedule-menu refreshes."""
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    menus_dirty = False
    while True:
        try:
            item = await redis.blpop(OUTBOX_KEY, timeout=1)
            if item is None:
                # Queue drained: refresh once per burst of changes instead of once per event.
                if menus_dirty:
                    menus_dirty = False
                    await refresh_all_schedule_menus(bot)
                continue
            event = json.loads(item[1])
            if event.get("type") == "notify":
                try:
                    await bot.send_message(event["telegram_id"], event["text"])
                except Exception as exc:
                    logger.info("Could not deliver notification to %s: %s", event.get("telegram_id"), exc)
            elif event.get("type") == "repeat_offer":
                try:
                    await bot.send_message(
                        event["telegram_id"],
                        event["text"],
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[[InlineKeyboardButton(text=event["button"], callback_data=f"repeat:{event['booking_id']}")]]
                        ),
                    )
                except Exception as exc:
                    logger.info("Could not deliver repeat offer to %s: %s", event.get("telegram_id"), exc)
            elif event.get("type") == "move_request":
                try:
                    await bot.send_message(
                        event["telegram_id"],
                        event["text"],
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(text=event["accept"], callback_data=f"move:accept:{event['request_id']}"),
                                    InlineKeyboardButton(text=event["decline"], callback_data=f"move:decline:{event['request_id']}"),
                                ]
                            ]
                        ),
                    )
                except Exception as exc:
                    logger.info("Could not deliver move request to %s: %s", event.get("telegram_id"), exc)
            elif event.get("type") == "waitlist_offer":
                try:
                    await bot.send_message(
                        event["telegram_id"],
                        event["text"],
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[[InlineKeyboardButton(text=event["button"], callback_data=f"waitlist:take:{event['entry_id']}")]]
                        ),
                    )
                except Exception as exc:
                    logger.info("Could not deliver waiting-list offer to %s: %s", event.get("telegram_id"), exc)
            elif event.get("type") == "schedule_changed":
                menus_dirty = True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Outbox worker error")
            await asyncio.sleep(2)


async def main() -> None:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is required")
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=settings.bot_token)
    dispatcher = Dispatcher(storage=RedisStorage.from_url(settings.redis_url))
    dispatcher.errors.register(on_error)
    # Feature routers go first: the main router has catch-all fallback handlers.
    dispatcher.include_router(planning_router)
    dispatcher.include_router(repeat_router)
    dispatcher.include_router(move_router)
    dispatcher.include_router(waitlist_router)
    dispatcher.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    worker = asyncio.create_task(outbox_worker(bot))
    menu_task = asyncio.create_task(setup_menu_button(bot))  # waits for the tunnel without delaying polling
    try:
        await dispatcher.start_polling(bot)
    finally:
        worker.cancel()
        menu_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
