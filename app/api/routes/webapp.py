"""JSON API of the Telegram Mini App. Authenticated by signed `initData`, not by the internal token."""
from datetime import date, time, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
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
from app.db.models import Band, Group, User
from app.db.session import get_session
from app.schemas.bands import (
    WebAppBandCreate,
    WebAppBandJoin,
    WebAppBandRename,
    WebAppMemberPick,
    WebAppMemberRole,
)
from app.schemas.bookings import (
    BookingRead,
    PriorityBookingResult,
    WebAppBookingCreate,
    WebAppBookingUpdate,
    WebAppPriorityCreate,
)
from app.schemas.move_requests import MoveRequestRead, WebAppMoveRequestCreate
from app.schemas.waitlist import WebAppWaitlistJoin
from app.services import bands as band_service
from app.services import bookings as booking_service
from app.services import move_requests as move_request_service
from app.services import waitlist as waitlist_service
from app.services.errors import NotFoundError
from app.services import roles as role_service
from app.services.permissions import can_manage_band, holds_admin_role, permission_summary
from app.services.schedule_history import person_name

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
    rows = await booking_service.list_schedule(session, viewer)
    closures = await booking_service.list_day_closures(session)
    window_start, window_end = await booking_service.get_booking_window_minutes(session)
    contacts_visible = viewer is not None  # contact details are for approved members opening the app from Telegram
    own_ids = (
        {row["id"] for row in await booking_service.list_my_booking_rows(session, viewer.telegram_id)} if contacts_visible else set()
    )
    if not contacts_visible:
        rows = [{**item, "responsible_name": None, "responsible_username": None} for item in rows]
    bookings = [
        {
            **{key: value for key, value in item.items() if key != "created_by" or contacts_visible},
            "band_name": "Личная репетиция"
            if item["purpose"] == "personal" and item["band_name"].startswith("Личная репетиция")
            else item["band_name"],
            "owner_person": None,  # the public grid never names who holds a personal rehearsal
            "booking_date": item["booking_date"].isoformat(),
            "start_time": item["start_time"].isoformat(),
            "end_time": item["end_time"].isoformat(),
            **({"is_mine": item["id"] in own_ids} if contacts_visible else {}),
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


@router.get("/my-bookings")
async def my_bookings(user: User = Depends(current_webapp_user), session: AsyncSession = Depends(get_session)) -> dict:
    """The viewer's upcoming bookings (made by them or by one of their bands), each with the right to change it."""
    rows = await booking_service.list_my_booking_rows(session, user.telegram_id)
    return {"bookings": [{**row, "booking_date": row["booking_date"].isoformat()} for row in rows]}


@router.patch("/bookings/{booking_id}", response_model=BookingRead)
async def update_booking(
    booking_id: int,
    payload: WebAppBookingUpdate,
    user: User = Depends(fresh_webapp_user),
    session: AsyncSession = Depends(get_session),
):
    return await booking_service.update_booking(
        session,
        user.telegram_id,
        booking_id,
        payload.booking_date,
        payload.start_time,
        payload.end_time,
        payload.purpose,
        payload.song_title,
    )


@router.delete("/bookings/{booking_id}")
async def cancel_booking(
    booking_id: int,
    user: User = Depends(fresh_webapp_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await booking_service.delete_booking(session, user.telegram_id, booking_id)


@router.get("/priority/preview")
async def priority_preview(
    booking_date: date,
    start_time: time,
    end_time: time,
    user: User = Depends(current_webapp_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Bookings a DSVR/admin takeover of this time would cancel; nothing is changed."""
    return {
        "bookings": await booking_service.preview_priority_booking(session, user.telegram_id, booking_date, start_time, end_time)
    }


@router.post("/priority", response_model=PriorityBookingResult)
async def create_priority_booking(
    payload: WebAppPriorityCreate,
    user: User = Depends(fresh_webapp_user),
    session: AsyncSession = Depends(get_session),
):
    return await booking_service.create_priority_booking(
        session, user.telegram_id, payload.booking_date, payload.start_time, payload.end_time, payload.title
    )


def _move_row(row: dict) -> dict:
    return {
        **row,
        "booking_date": row["booking_date"].isoformat(),
        "start_time": row["start_time"].isoformat(),
        "end_time": row["end_time"].isoformat(),
        "proposed_date": row["proposed_date"].isoformat(),
        "proposed_start": row["proposed_start"].isoformat(),
        "proposed_end": row["proposed_end"].isoformat(),
        "expires_at": row["expires_at"].isoformat(timespec="minutes"),
    }


@router.get("/move-requests")
async def move_requests(user: User = Depends(current_webapp_user), session: AsyncSession = Depends(get_session)) -> dict:
    """Open "please move" requests: those the viewer can answer and those they sent."""
    found = await move_request_service.list_for_user(session, user.telegram_id)
    return {key: [_move_row(row) for row in rows] for key, rows in found.items()}


@router.post("/move-requests", response_model=MoveRequestRead)
async def create_move_request(
    payload: WebAppMoveRequestCreate,
    user: User = Depends(fresh_webapp_user),
    session: AsyncSession = Depends(get_session),
):
    return await move_request_service.create_request(
        session, user.telegram_id, payload.booking_id, payload.proposed_date, payload.start_time, payload.end_time, payload.message
    )


@router.post("/move-requests/{request_id}/accept", response_model=MoveRequestRead)
async def accept_move_request(
    request_id: int, user: User = Depends(fresh_webapp_user), session: AsyncSession = Depends(get_session)
):
    return await move_request_service.accept(session, user.telegram_id, request_id)


@router.post("/move-requests/{request_id}/decline", response_model=MoveRequestRead)
async def decline_move_request(
    request_id: int, user: User = Depends(fresh_webapp_user), session: AsyncSession = Depends(get_session)
):
    return await move_request_service.decline(session, user.telegram_id, request_id)


@router.post("/move-requests/{request_id}/cancel", response_model=MoveRequestRead)
async def cancel_move_request(
    request_id: int, user: User = Depends(fresh_webapp_user), session: AsyncSession = Depends(get_session)
):
    return await move_request_service.cancel(session, user.telegram_id, request_id)


@router.get("/waitlist")
async def waitlist(user: User = Depends(current_webapp_user), session: AsyncSession = Depends(get_session)) -> dict:
    """The viewer's waiting-list entries; `is_free` marks the ones that can be booked right now."""
    rows = await waitlist_service.list_for_user(session, user.telegram_id)
    return {
        "entries": [
            {
                **row,
                "booking_date": row["booking_date"].isoformat(),
                "start_time": row["start_time"].isoformat(),
                "end_time": row["end_time"].isoformat(),
            }
            for row in rows
        ]
    }


@router.post("/waitlist")
async def join_waitlist(
    payload: WebAppWaitlistJoin, user: User = Depends(fresh_webapp_user), session: AsyncSession = Depends(get_session)
) -> dict:
    entry = await waitlist_service.join(
        session, user.telegram_id, payload.band_id, payload.booking_date, payload.start_time, payload.end_time
    )
    return {"id": entry.id}


@router.post("/waitlist/{entry_id}/leave")
async def leave_waitlist(
    entry_id: int, user: User = Depends(fresh_webapp_user), session: AsyncSession = Depends(get_session)
) -> dict:
    await waitlist_service.leave(session, user.telegram_id, entry_id)
    return {"id": entry_id}


@router.post("/waitlist/{entry_id}/take", response_model=BookingRead)
async def take_waitlist(
    entry_id: int, user: User = Depends(fresh_webapp_user), session: AsyncSession = Depends(get_session)
):
    return await waitlist_service.take(session, user.telegram_id, entry_id)


async def _band_payload(session: AsyncSession, viewer: User, band: Band, invite_visible: bool) -> dict:
    members = [
        {
            "id": member.id,
            "name": person_name(member.user),
            "study_group": member.user.study_group,
            "telegram_username": member.user.telegram_username,
            "instrument_role": member.instrument_role,
            "is_owner": member.is_owner,
            "is_responsible": member.user_id == band.responsible_user_id,
            "is_me": member.user_id == viewer.id,
        }
        for member in sorted(band.members, key=lambda item: (not item.is_owner, item.id))
    ]
    return {
        "id": band.id,
        "name": band.name,
        "group_title": band.group.title if band.group is not None else None,
        "invite_code": band.invite_code if invite_visible else None,
        "can_manage": await can_manage_band(session, viewer, band),
        "members": members,
    }


async def _bands_payload(session: AsyncSession, viewer: User, bands: list[Band]) -> list[dict]:
    visible = await band_service.invite_code_visible_ids(session, viewer.telegram_id, bands)
    return [await _band_payload(session, viewer, band, band.id in visible) for band in bands]


@router.get("/bands")
async def bands(user: User = Depends(current_webapp_user), session: AsyncSession = Depends(get_session)) -> dict:
    """The viewer's bands with their members; the join code only for the people who run the band."""
    return {"bands": await _bands_payload(session, user, await band_service.list_user_bands(session, user.telegram_id))}


async def _one_band(session: AsyncSession, user: User, band_id: int) -> dict:
    await band_service.get_band_for_requester(session, band_id, user.telegram_id)  # visibility check
    # The session does not expire rows on commit: re-read the band so edits made a moment ago show up.
    fresh = await session.execute(
        select(Band)
        .where(Band.id == band_id)
        .options(*band_service.band_load_options())
        .execution_options(populate_existing=True)
    )
    return (await _bands_payload(session, user, [fresh.scalar_one()]))[0]


@router.post("/bands")
async def create_band(
    payload: WebAppBandCreate,
    user: User = Depends(fresh_webapp_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    band = await band_service.create_band(session, user.telegram_id, payload.name, payload.instrument_role)
    return await _one_band(session, user, band.id)


@router.post("/bands/join")
async def join_band(
    payload: WebAppBandJoin,
    user: User = Depends(fresh_webapp_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    band = await band_service.join_band(session, user.telegram_id, payload.invite_code, payload.instrument_role)
    return await _one_band(session, user, band.id)


@router.patch("/bands/{band_id}")
async def rename_band(
    band_id: int,
    payload: WebAppBandRename,
    user: User = Depends(fresh_webapp_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await band_service.update_band_name(session, band_id, user.telegram_id, payload.name)
    return await _one_band(session, user, band_id)


@router.patch("/bands/{band_id}/owner")
async def transfer_owner(
    band_id: int,
    payload: WebAppMemberPick,
    user: User = Depends(fresh_webapp_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await band_service.transfer_band_owner(session, band_id, user.telegram_id, payload.member_id)
    return await _one_band(session, user, band_id)


@router.patch("/bands/{band_id}/responsible")
async def set_responsible(
    band_id: int,
    payload: WebAppMemberPick,
    user: User = Depends(fresh_webapp_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await band_service.update_band_responsible(session, band_id, user.telegram_id, payload.member_id)
    return await _one_band(session, user, band_id)


@router.patch("/bands/{band_id}/members/{member_id}")
async def set_member_role(
    band_id: int,
    member_id: int,
    payload: WebAppMemberRole,
    user: User = Depends(fresh_webapp_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await band_service.update_member_role(session, band_id, member_id, user.telegram_id, payload.instrument_role)
    return await _one_band(session, user, band_id)


@router.delete("/bands/{band_id}/members/{member_id}")
async def remove_member(
    band_id: int,
    member_id: int,
    user: User = Depends(fresh_webapp_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await band_service.remove_band_member(session, band_id, member_id, user.telegram_id)


@router.delete("/bands/{band_id}")
async def delete_band(
    band_id: int,
    user: User = Depends(fresh_webapp_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await band_service.delete_band(session, band_id, user.telegram_id)


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
            "roles": [],
            "permissions": {},
        }
    group = await session.get(Group, user.group_id) if user.group_id else None
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
        "is_admin": await holds_admin_role(session, user),
        "roles": [
            {
                "name": item.role.name,
                "group": {"id": item.group.id, "slug": item.group.slug, "title": item.group.title} if item.group else None,
            }
            for item in await role_service.user_assignments(session, user)
        ],
        "permissions": await permission_summary(session, user),
    }


@router.get("/locales/{language}")
async def locale(language: str) -> dict[str, str]:
    """Mini App strings from the bot's locale files (keys under `webapp.`); missing translations fall back to Russian."""
    if language not in LANGUAGES:
        raise NotFoundError("Unknown language")
    strings = {key: value for key, value in load_locale("ru").items() if key.startswith(LOCALE_NAMESPACE)}
    strings.update({key: value for key, value in load_locale(language).items() if key.startswith(LOCALE_NAMESPACE)})
    return strings
