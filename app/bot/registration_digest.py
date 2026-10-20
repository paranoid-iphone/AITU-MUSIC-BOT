import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.bot.api_client import BackendClient
from app.bot.keyboards import registration_open_keyboard
from app.bot.webapp_url import get_webapp_base_url

logger = logging.getLogger(__name__)


async def send_registration_digest(bot: Bot, api: BackendClient, mode: str, base_url: str | None) -> int:
    """One message per moderator with the number of requests waiting for them, and a button to the requests tab.

    Requests that arrived while the bot was down never got their own notice; this is the catch-up. Returns how many
    messages went out. The club setting "off" keeps it quiet like the per-request notices."""
    if mode == "off":
        return 0
    sent = 0
    for item in await api.registration_digest():
        try:
            await bot.send_message(
                item["telegram_id"],
                f"Заявок на регистрацию ждут решения: {item['pending']}.",
                reply_markup=registration_open_keyboard(base_url),
            )
            sent += 1
        except TelegramAPIError as exc:
            logger.warning("Could not send the registration digest to %s: %s", item["telegram_id"], exc)
    return sent


async def send_registration_digest_on_start(bot: Bot, api: BackendClient, attempts: int = 6, delay: float = 10.0) -> None:
    """Run the digest once the backend answers (it may still be starting) and the public URL is known (the tunnel
    may come up after the bot); without a URL the message goes out without the button."""
    for attempt in range(attempts):
        try:
            mode = await api.registration_notify_mode()
            base_url = await get_webapp_base_url()
            if base_url is None and attempt + 1 < attempts:
                await asyncio.sleep(delay)
                continue
            sent = await send_registration_digest(bot, api, mode, base_url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Registration digest attempt %s failed: %s", attempt + 1, exc)
            if attempt + 1 < attempts:
                await asyncio.sleep(delay)
            continue
        if sent:
            logger.info("Sent the registration digest to %s moderator(s)", sent)
        return
