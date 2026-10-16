import asyncio
import json
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis

from app.bot.handlers import refresh_all_schedule_menus, router
from app.bot.handlers_dsvr import router as dsvr_router
from app.bot.handlers_history import router as history_router
from app.bot.handlers_planning import router as planning_router
from app.bot.handlers_groups import router as groups_router
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
    # Feature routers go first: the main router has catch-all fallback handlers.
    dispatcher.include_router(groups_router)
    dispatcher.include_router(history_router)
    dispatcher.include_router(dsvr_router)
    dispatcher.include_router(planning_router)
    dispatcher.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    worker = asyncio.create_task(outbox_worker(bot))
    try:
        await dispatcher.start_polling(bot)
    finally:
        worker.cancel()


if __name__ == "__main__":
    asyncio.run(main())
