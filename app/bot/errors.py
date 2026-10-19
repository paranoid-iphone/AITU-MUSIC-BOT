import logging

from aiogram.types import ErrorEvent

from app.bot.api_client import BackendUnavailable
from app.bot.handlers import answer_callback
from app.bot.i18n import t

logger = logging.getLogger(__name__)


def error_text(exception: BaseException) -> str:
    if isinstance(exception, BackendUnavailable):
        return str(exception)
    if isinstance(exception, RuntimeError):  # BackendClient turns 4xx answers into RuntimeError(detail)
        return t("ru", "error_failed").format(detail=exception)
    return t("ru", "error_generic")


async def on_error(event: ErrorEvent) -> bool:
    """Last resort for handlers that let an exception escape: log it and tell the user instead of leaving a spinner."""
    update = event.update
    logger.error("Unhandled error in update %s", update.update_id, exc_info=event.exception)
    text = error_text(event.exception)
    try:
        if update.callback_query is not None:
            await answer_callback(update.callback_query, text, show_alert=True)
        elif update.message is not None:
            await update.message.answer(text)
    except Exception as exc:  # message deleted, query too old, user blocked the bot
        logger.info("Could not report an error to the user: %s", exc)
    return True
