"""Authentication of Telegram Mini App requests via the signed `initData` string.

The Mini App is reachable through the public tunnel, so it never uses the internal API token: the viewer proves who
they are with the HMAC-signed `X-Telegram-Init-Data` header that Telegram hands to the page.
"""
from dataclasses import dataclass

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.telegram_auth import validate_init_data
from app.db.models import User, UserStatus
from app.db.session import get_session
from app.services.errors import ForbiddenError, UnauthorizedError
from app.services.users import get_user_by_telegram_id

READ_MAX_AGE_SECONDS = 24 * 60 * 60
# Mutations demand a recent sign-in so a leaked or left-over initData string cannot change the schedule for long.
MUTATION_MAX_AGE_SECONDS = 60 * 60


@dataclass
class WebAppIdentity:
    telegram_user: dict
    user: User | None


async def _identity(session: AsyncSession, init_data: str | None, max_age_seconds: int) -> WebAppIdentity | None:
    telegram_user = validate_init_data(init_data, settings.bot_token, max_age_seconds)
    if telegram_user is None:
        return None
    try:
        telegram_id = int(telegram_user["id"])
    except (TypeError, ValueError):
        return None
    return WebAppIdentity(telegram_user, await get_user_by_telegram_id(session, telegram_id))


async def webapp_identity(
    x_telegram_init_data: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> WebAppIdentity:
    """Any viewer with a valid signature, registered or not (used by `/me`, which reports the status)."""
    identity = await _identity(session, x_telegram_init_data, READ_MAX_AGE_SECONDS)
    if identity is None:
        raise UnauthorizedError("Invalid or expired Telegram session")
    return identity


async def optional_webapp_user(
    x_telegram_init_data: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> User | None:
    """The approved member behind the request, or None for anyone else. Never raises (public read endpoints)."""
    identity = await _identity(session, x_telegram_init_data, READ_MAX_AGE_SECONDS)
    if identity is None or identity.user is None or identity.user.status != UserStatus.APPROVED.value:
        return None
    return identity.user


def _require_approved(identity: WebAppIdentity | None) -> User:
    if identity is None:
        raise UnauthorizedError("Invalid or expired Telegram session")
    if identity.user is None or identity.user.status != UserStatus.APPROVED.value:
        raise ForbiddenError("Your registration is not approved")
    return identity.user


async def current_webapp_user(
    x_telegram_init_data: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Approved member, for reading personal data."""
    return _require_approved(await _identity(session, x_telegram_init_data, READ_MAX_AGE_SECONDS))


async def fresh_webapp_user(
    x_telegram_init_data: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Approved member whose sign-in is less than an hour old, for anything that changes data."""
    return _require_approved(await _identity(session, x_telegram_init_data, MUTATION_MAX_AGE_SECONDS))
