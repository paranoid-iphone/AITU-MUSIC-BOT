import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis

from app.bot.errors import on_error
from app.bot.handlers import api, refresh_all_schedule_menus, router
from app.bot.handlers_planning import router as planning_router
from app.bot.handlers_move import router as move_router
from app.bot.handlers_repeat import router as repeat_router
from app.bot.handlers_waitlist import router as waitlist_router
from app.bot.menu_button import setup_menu_button
from app.bot.menu_refresh import MenuRefresher
from app.bot.outbox_delivery import process_next, recover_processing
from app.bot.registration_digest import send_registration_digest_on_start
from app.core.config import settings
from app.core.logging_setup import setup_logging


logger = logging.getLogger(__name__)


async def outbox_worker(bot: Bot) -> None:
    """Deliver events queued by the backend: personal notifications and schedule-menu refreshes."""
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    refresher = MenuRefresher(lambda: refresh_all_schedule_menus(bot))
    try:
        await _outbox_loop(bot, redis, refresher)
    finally:
        refresher.close()


async def _outbox_loop(bot: Bot, redis: Redis, refresher: MenuRefresher) -> None:
    restored = False
    while True:
        try:
            if not restored:
                count = await recover_processing(redis)
                restored = True
                if count:
                    logger.info("Restored %s unfinished outbox event(s)", count)
            if not await process_next(bot, redis, refresher):
                # Queue drained: refresh once per burst of changes instead of once per event, in the background.
                refresher.poll()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Outbox worker error")
            restored = False  # an event taken just before the failure is still in the processing list
            await asyncio.sleep(2)


async def main() -> None:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is required")
    setup_logging()
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
    digest_task = asyncio.create_task(send_registration_digest_on_start(bot, api))  # requests that came in while the bot was down
    try:
        await dispatcher.start_polling(bot)
    finally:
        worker.cancel()
        menu_task.cancel()
        digest_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
