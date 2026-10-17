"""JSON API of the Telegram Mini App. Authenticated by signed `initData`, not by the internal token."""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.webapp_auth import (
    WebAppIdentity,
    current_webapp_user,
    fresh_webapp_user,
    optional_webapp_user,
    webapp_identity,
)
from app.bot.i18n import load_locale
from app.core.config import settings
from app.core.timezone import now_local
from app.db.models import Group, User
from app.db.session import get_session
from app.schemas.bookings import BookingRead, WebAppBookingCreate
from app.services import bands as band_service
from app.services import bookings as booking_service
from app.services.errors import NotFoundError
from app.services.permissions import administered_group_ids, is_dsvr, is_global_admin

router = APIRouter(prefix="/webapp/api", tags=["webapp"])

SCHEDULE_DAYS = 7
SLOT_MINUTES = 30
LOCALE_NAMESPACE = "webapp."
LANGUAGES = ("ru", "en", "kz")
# Telegram client language codes that differ from the bot's own language codes.
TELEGRAM_LANGUAGE_ALIASES = {"kk": "kz"}


def _telegram_language(code: str | None) -> str:
    prefix = (code or "").lower()[:2]
    language = TELEGRAM_LANGUAGE_ALIASES.get(prefix, prefix)
    return language if language in LANGUAGES else settings.default_language


@router.get("/schedule")
async def schedule(
    session: AsyncSession = Depends(get_session),
    viewer: User | None = Depends(optional_webapp_user),
) -> dict:
    """The sliding 7-day window. The club clock (not the browser's) decides what "today" is."""
    now = now_local()
    rows = await booking_service.list_schedule(session)
    closures = await booking_service.list_day_closures(session)
    window_start, window_end = await booking_service.get_booking_window_minutes(session)
    contacts_visible = viewer is not None  # contact details are for approved members opening the app from Telegram
    if not contacts_visible:
        rows = [{**item, "responsible_name": None, "responsible_username": None} for item in rows]
    bookings = [
        {
            **{key: value for key, value in item.items() if key != "created_by" or contacts_visible},
            "band_name": "Личная репетиция"
            if item["purpose"] == "personal" and item["band_name"].startswith("Личная репетиция")
            else item["band_name"],
            "booking_date": item["booking_date"].isoformat(),
            "start_time": item["start_time"].isoformat(),
            "end_time": item["end_time"].isoformat(),
        }
        for item in rows
    ]
    days = [now.date() + timedelta(days=offset) for offset in range(SCHEDULE_DAYS)]
    return {
        "now": now.isoformat(timespec="minutes"),
        "today": now.date().isoformat(),
        "days": [{"date": day.isoformat(), "weekday": day.weekday()} for day in days],
        "slot_minutes": SLOT_MINUTES,
        "window_start_minutes": window_start,
        "window_end_minutes": window_end,
        "contacts_visible": contacts_visible,
        "bookings": bookings,
        "day_closures": [{**item, "date": item["date"].isoformat()} for item in closures],
        "free_slots": await _free_slots(session, days),
    }


async def _free_slots(session: AsyncSession, days: list[date]) -> dict[str, list[str]]:
    """Start times of bookable half-hour cells per day: the booking rules (past, closed days, hours) decide, not the page."""
    free = {}
    for day in days:
        slots = await booking_service.list_available_slots(session, day, SLOT_MINUTES)
        free[day.isoformat()] = [slot["start_time"].strftime("%H:%M") for slot in slots]
    return free


@router.get("/my-bands")
async def my_bands(user: User = Depends(current_webapp_user), session: AsyncSession = Depends(get_session)) -> dict:
    """Bands the viewer can book for, and whether personal rehearsals are switched on."""
    bands = await band_service.list_user_bands(session, user.telegram_id)
    return {
        "bands": [
            {"id": band.id, "name": band.name, "group_title": band.group.title if band.group is not None else None}
            for band in bands
        ],
        "personal_enabled": await booking_service.personal_bookings_are_enabled(session),
    }


@router.get("/limits")
async def limits(
    band_id: int,
    target_date: date | None = Query(default=None, alias="date"),
    user: User = Depends(current_webapp_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Weekly and daily booking time left for one of the viewer's bands."""
    return await booking_service.get_booking_limit_status(session, user.telegram_id, band_id, target_date)


@router.post("/bookings", response_model=BookingRead)
async def create_booking(
    payload: WebAppBookingCreate,
    user: User = Depends(fresh_webapp_user),
    session: AsyncSession = Depends(get_session),
):
    return await booking_service.create_booking(
        session,
        user.telegram_id,
        payload.band_id,
        payload.booking_date,
        payload.start_time,
        payload.end_time,
        payload.purpose,
        payload.song_title,
    )


@router.get("/me")
async def me(
    identity: WebAppIdentity = Depends(webapp_identity),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Who is looking: profile, status, roles and language. Also answers for people who have not registered yet."""
    user = identity.user
    if user is None:
        return {
            "registered": False,
            "status": None,
            "language": _telegram_language(identity.telegram_user.get("language_code")),
            "telegram_id": int(identity.telegram_user["id"]),
            "first_name": identity.telegram_user.get("first_name"),
            "last_name": identity.telegram_user.get("last_name"),
            "telegram_username": identity.telegram_user.get("username"),
            "group": None,
            "is_admin": False,
            "is_dsvr": False,
            "administered_groups": [],
        }
    group = await session.get(Group, user.group_id) if user.group_id else None
    administered = []
    for group_id in await administered_group_ids(session, user):
        item = await session.get(Group, group_id)
        if item is not None:
            administered.append({"id": item.id, "slug": item.slug, "title": item.title})
    return {
        "registered": True,
        "status": user.status,
        "language": user.language if user.language in LANGUAGES else settings.default_language,
        "telegram_id": user.telegram_id,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "study_group": user.study_group,
        "telegram_username": user.telegram_username,
        "profile_completed": user.profile_completed,
        "group": {"id": group.id, "slug": group.slug, "title": group.title} if group else None,
        "is_admin": is_global_admin(user),
        "is_dsvr": is_dsvr(user),
        "administered_groups": administered,
    }


@router.get("/locales/{language}")
async def locale(language: str) -> dict[str, str]:
    """Mini App strings from the bot's locale files (keys under `webapp.`); missing translations fall back to Russian."""
    if language not in LANGUAGES:
        raise NotFoundError("Unknown language")
    strings = {key: value for key, value in load_locale("ru").items() if key.startswith(LOCALE_NAMESPACE)}
    strings.update({key: value for key, value in load_locale(language).items() if key.startswith(LOCALE_NAMESPACE)})
    return strings
