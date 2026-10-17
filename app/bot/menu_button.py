import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import MenuButtonWebApp, WebAppInfo

from app.bot.webapp_url import get_webapp_base_url

logger = logging.getLogger(__name__)

MENU_BUTTON_TEXT = "📅 Расписание"


async def setup_menu_button(bot: Bot, attempts: int = 10, delay: float = 6.0) -> bool:
    """Point the bot's chat menu button at the Mini App.

    The ngrok tunnel can come up after the bot, so the public URL is polled for a while before giving up.
    """
    for attempt in range(attempts):
        base_url = await get_webapp_base_url()
        if base_url:
            try:
                await bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(text=MENU_BUTTON_TEXT, web_app=WebAppInfo(url=f"{base_url}/webapp/schedule"))
                )
            except TelegramAPIError as exc:
                logger.warning("Could not set the Mini App menu button: %s", exc)
                return False
            logger.info("Mini App menu button set to %s/webapp/schedule", base_url)
            return True
        if attempt + 1 < attempts:
            await asyncio.sleep(delay)
    logger.warning("Mini App menu button not set: no public URL (set WEBAPP_BASE_URL or start the ngrok tunnel)")
    return False
