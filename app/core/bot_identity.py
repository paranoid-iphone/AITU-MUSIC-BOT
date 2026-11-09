"""The bot's @username, which the backend needs to build t.me links (band invite links).

Asked from Telegram (`getMe` with BOT_TOKEN) on first use and kept for the life of the process. A failed attempt is
retried only after RETRY_SECONDS, so an unreachable Telegram does not slow every request down; until then the links
are simply not offered.
"""
import logging
import time

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

RETRY_SECONDS = 60
TIMEOUT_SECONDS = 5


class _State:
    username: str | None = None
    failed_at: float | None = None


_state = _State()


async def _fetch_username() -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        response = await client.get(f"https://api.telegram.org/bot{settings.bot_token}/getMe")
    response.raise_for_status()
    return response.json()["result"]["username"]


async def bot_username() -> str | None:
    if _state.username is not None or not settings.bot_token:
        return _state.username
    if _state.failed_at is not None and time.monotonic() - _state.failed_at < RETRY_SECONDS:
        return None
    try:
        _state.username = await _fetch_username()
        _state.failed_at = None
    except Exception as exc:  # the message of an httpx error holds the URL, i.e. the token: log only the type
        _state.failed_at = time.monotonic()
        logger.warning("Could not get the bot username from Telegram (%s); invite links are hidden for now", type(exc).__name__)
    return _state.username


def invite_link(username: str | None, invite_code: str | None) -> str | None:
    """`https://t.me/<bot>?start=join_<code>`: opens the bot, whose /start offers the Mini App join form."""
    if not username or not invite_code:
        return None
    return f"https://t.me/{username}?start=join_{invite_code}"
