"""Mini App HTTP API: schedule, /me, locales, static files and the bot's menu button."""
from datetime import datetime, time

import httpx
import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SetChatMenuButton

from app.bot import menu_button
from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.services import bookings
from tests.test_webapp_auth import BOT_TOKEN, init_data

NOW = datetime(2026, 10, 14, 12, 0)  # a Wednesday


@pytest_asyncio.fixture
async def client(session, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", BOT_TOKEN)

    async def _session_override():
        yield session

    app.dependency_overrides[get_session] = _session_override
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


def tg(telegram_id: int, **extra: str) -> dict[str, str]:
    return {"X-Telegram-Init-Data": init_data(telegram_id, **extra)}


async def booked_schedule(session, make_user, make_band, freeze_now):
    freeze_now(NOW)
    leader = await make_user(last_name="Петров", first_name="Иван", telegram_username="ivan")
    band = await make_band(leader, "Rockers")
    await bookings.create_booking(session, leader.telegram_id, band.id, NOW.date(), time(15), time(16), "self", "Song")
    return leader


async def test_schedule_hides_contacts_without_a_telegram_signature(client, session, make_user, make_band, freeze_now):
    await booked_schedule(session, make_user, make_band, freeze_now)
    payload = (await client.get("/webapp/api/schedule")).json()
    assert payload["contacts_visible"] is False
    booking = payload["bookings"][0]
    assert booking["responsible_name"] is None and booking["responsible_username"] is None
    assert "created_by" not in booking


async def test_schedule_shows_contacts_to_approved_members_only(client, session, make_user, make_band, freeze_now):
    leader = await booked_schedule(session, make_user, make_band, freeze_now)
    payload = (await client.get("/webapp/api/schedule", headers=tg(leader.telegram_id))).json()
    assert payload["contacts_visible"] is True
    assert payload["bookings"][0]["responsible_username"] == "ivan"

    pending = await make_user(status="pending")
    payload = (await client.get("/webapp/api/schedule", headers=tg(pending.telegram_id))).json()
    assert payload["contacts_visible"] is False
    assert payload["bookings"][0]["responsible_username"] is None

    forged = (await client.get("/webapp/api/schedule", headers={"X-Telegram-Init-Data": "hash=forged"})).json()
    assert forged["contacts_visible"] is False


async def test_schedule_reports_club_today_and_grid_bounds(client, session, freeze_now):
    freeze_now(NOW)
    payload = (await client.get("/webapp/api/schedule")).json()
    assert payload["today"] == "2026-10-14"
    assert [day["date"] for day in payload["days"]][:2] == ["2026-10-14", "2026-10-15"]
    assert len(payload["days"]) == 7
    assert payload["days"][0]["weekday"] == 2  # Wednesday, Monday = 0
    assert (payload["slot_minutes"], payload["window_start_minutes"], payload["window_end_minutes"]) == (30, 480, 1260)


async def test_me_requires_a_valid_signature(client):
    assert (await client.get("/webapp/api/me")).status_code == 401
    assert (await client.get("/webapp/api/me", headers={"X-Telegram-Init-Data": "hash=forged"})).status_code == 401


async def test_me_for_an_approved_member(client, make_user):
    user = await make_user(language="kz")
    payload = (await client.get("/webapp/api/me", headers=tg(user.telegram_id))).json()
    assert payload["registered"] is True and payload["status"] == "approved"
    assert payload["language"] == "kz"
    assert payload["group"]["slug"] == "music"
    assert (payload["is_admin"], payload["is_dsvr"], payload["administered_groups"]) == (False, False, [])


async def test_me_reports_roles(client, make_user, session):
    from sqlalchemy import select

    from app.db import models

    admin = await make_user(role="admin")
    dsvr = await make_user(role="dsvr")
    group_admin = await make_user()
    dance = (await session.execute(select(models.Group).where(models.Group.slug == "dance"))).scalar_one()
    session.add(models.GroupAdmin(group_id=dance.id, user_id=group_admin.id))
    await session.commit()

    assert (await client.get("/webapp/api/me", headers=tg(admin.telegram_id))).json()["is_admin"] is True
    assert (await client.get("/webapp/api/me", headers=tg(dsvr.telegram_id))).json()["is_dsvr"] is True
    groups = (await client.get("/webapp/api/me", headers=tg(group_admin.telegram_id))).json()["administered_groups"]
    assert [group["slug"] for group in groups] == ["dance"]


async def test_me_for_a_pending_member_reports_the_status(client, make_user):
    user = await make_user(status="pending")
    payload = (await client.get("/webapp/api/me", headers=tg(user.telegram_id))).json()
    assert payload["registered"] is True and payload["status"] == "pending"
    assert (payload["is_admin"], payload["is_dsvr"]) == (False, False)


async def test_me_for_an_unregistered_telegram_user(client):
    fields = {"user": '{"id": 424242, "first_name": "Aigerim", "language_code": "kk"}'}
    payload = (await client.get("/webapp/api/me", headers={"X-Telegram-Init-Data": init_data(424242, **fields)})).json()
    assert payload["registered"] is False and payload["status"] is None
    assert payload["language"] == "kz"  # Telegram says "kk"


async def test_locales_expose_only_webapp_keys(client):
    for language in ("ru", "en", "kz"):
        strings = (await client.get(f"/webapp/api/locales/{language}")).json()
        assert strings and all(key.startswith("webapp.") for key in strings)
    ru = (await client.get("/webapp/api/locales/ru")).json()
    en = (await client.get("/webapp/api/locales/en")).json()
    assert ru["webapp.free"] == "свободно" and en["webapp.free"] == "free"
    assert set(en) == set(ru)
    assert [ru[f"webapp.weekday.{index}"] for index in range(7)][:2] == ["Пн", "Вт"]


async def test_locale_falls_back_to_russian_and_rejects_unknown_languages(client, monkeypatch):
    from app.bot import i18n

    original = i18n.load_locale("kz")
    trimmed = {key: value for key, value in original.items() if key != "webapp.free"}
    monkeypatch.setattr("app.api.routes.webapp.load_locale", lambda language: trimmed if language == "kz" else original if language == "ru" else {})
    assert (await client.get("/webapp/api/locales/kz")).json()["webapp.free"] == original["webapp.free"]
    assert (await client.get("/webapp/api/locales/de")).status_code == 404


async def test_webapp_pages_are_served_without_the_internal_token(client):
    for path in ("/webapp/", "/webapp/schedule"):
        response = await client.get(path)
        assert response.status_code == 200 and "text/html" in response.headers["content-type"]
        assert response.headers["cache-control"] == "no-cache"
        assert "/webapp/static/app.js" in response.text
    for asset in ("app.js", "app.css"):
        response = await client.get(f"/webapp/static/{asset}")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-cache"


async def test_tunnel_traffic_reaches_only_the_webapp(client, session):
    tunnel = {"X-Forwarded-Host": "club.ngrok.app"}
    assert (await client.get("/webapp/api/schedule", headers=tunnel)).status_code == 200
    assert (await client.get("/webapp/static/app.js", headers=tunnel)).status_code == 200
    assert (await client.get("/planning/week", headers=tunnel)).status_code == 404


class FakeBot:
    def __init__(self, error: Exception | None = None):
        self.calls = []
        self.error = error

    async def set_chat_menu_button(self, **kwargs):
        if self.error:
            raise self.error
        self.calls.append(kwargs["menu_button"])


async def test_menu_button_points_at_the_mini_app(monkeypatch):
    async def base_url():
        return "https://club.example"

    monkeypatch.setattr(menu_button, "get_webapp_base_url", base_url)
    bot = FakeBot()
    assert await menu_button.setup_menu_button(bot) is True
    assert bot.calls[0].web_app.url == "https://club.example/webapp/schedule"


async def test_menu_button_waits_for_the_tunnel_then_gives_up(monkeypatch):
    urls = iter([None, None, "https://club.example"])

    async def base_url():
        return next(urls, None)

    monkeypatch.setattr(menu_button, "get_webapp_base_url", base_url)
    bot = FakeBot()
    assert await menu_button.setup_menu_button(bot, attempts=5, delay=0) is True
    assert len(bot.calls) == 1

    async def never():
        return None

    monkeypatch.setattr(menu_button, "get_webapp_base_url", never)
    assert await menu_button.setup_menu_button(FakeBot(), attempts=3, delay=0) is False


async def test_menu_button_survives_a_telegram_error(monkeypatch):
    async def base_url():
        return "http://insecure.example"

    monkeypatch.setattr(menu_button, "get_webapp_base_url", base_url)
    error = TelegramBadRequest(method=SetChatMenuButton(), message="Bad Request: only https links are allowed")
    assert await menu_button.setup_menu_button(FakeBot(error)) is False


# ---------- step 6: booking from the Mini App ----------


def booking_body(band_id, day="2026-10-15", start="10:00", end="11:00", **extra):
    return {"band_id": band_id, "booking_date": day, "start_time": start, "end_time": end, "purpose": "self", **extra}


async def test_schedule_lists_free_half_hour_cells_and_skips_taken_and_past_ones(client, session, make_user, make_band, freeze_now):
    await booked_schedule(session, make_user, make_band, freeze_now)  # today 15:00-16:00 is taken, "now" is 12:00
    free = (await client.get("/webapp/api/schedule")).json()["free_slots"]
    assert set(free) == {f"2026-10-{day}" for day in range(14, 21)}
    today = free["2026-10-14"]
    assert "12:00" not in today and "12:30" in today  # the past is not bookable
    assert "15:00" not in today and "15:30" not in today  # the booked hour
    assert "16:00" in today
    assert free["2026-10-15"][0] == "08:00" and free["2026-10-15"][-1] == "20:30"


async def test_schedule_free_cells_skip_closed_days(client, session, make_user, freeze_now):
    from app.services import admin as admin_service

    freeze_now(NOW)
    admin = await make_user(role="admin")
    await admin_service.update_available_day(session, admin.telegram_id, 3, False, "другой клуб")  # Thursday
    free = (await client.get("/webapp/api/schedule")).json()["free_slots"]
    assert free["2026-10-15"] == []


async def test_schedule_shows_who_booked_to_approved_members_only(client, session, make_user, make_band, freeze_now):
    leader = await booked_schedule(session, make_user, make_band, freeze_now)
    anonymous = (await client.get("/webapp/api/schedule")).json()["bookings"][0]
    assert "created_by" not in anonymous
    member = (await client.get("/webapp/api/schedule", headers=tg(leader.telegram_id))).json()["bookings"][0]
    assert member["created_by"] == "Петров Иван"


async def test_my_bands_lists_only_own_bands_and_personal_flag(client, session, make_user, make_band, freeze_now):
    freeze_now(NOW)
    leader = await make_user()
    other = await make_user()
    mine = await make_band(leader, "Rockers")
    await make_band(other, "Strangers")
    payload = (await client.get("/webapp/api/my-bands", headers=tg(leader.telegram_id))).json()
    assert [band["id"] for band in payload["bands"]] == [mine.id]
    assert payload["bands"][0]["name"] == "Rockers"
    assert payload["personal_enabled"] is True


async def test_my_bands_and_limits_need_an_approved_member(client, make_user):
    pending = await make_user(status="pending")
    assert (await client.get("/webapp/api/my-bands")).status_code == 401
    assert (await client.get("/webapp/api/my-bands", headers=tg(pending.telegram_id))).status_code == 403
    assert (await client.get("/webapp/api/limits?band_id=1", headers=tg(pending.telegram_id))).status_code == 403


async def test_limits_report_the_remaining_time_and_refuse_foreign_bands(client, session, make_user, make_band, freeze_now):
    freeze_now(NOW)
    leader = await make_user()
    stranger = await make_user()
    band = await make_band(leader, "Rockers")
    limits = (await client.get(f"/webapp/api/limits?band_id={band.id}&date=2026-10-15", headers=tg(leader.telegram_id))).json()
    assert limits["remaining_minutes"] == limits["weekly_limit_minutes"] > 0
    assert (await client.get(f"/webapp/api/limits?band_id={band.id}", headers=tg(stranger.telegram_id))).status_code == 403


async def test_member_books_a_slot_for_their_band(client, session, make_user, make_band, freeze_now):
    freeze_now(NOW)
    leader = await make_user()
    band = await make_band(leader, "Rockers")
    response = await client.post(
        "/webapp/api/bookings", json=booking_body(band.id, song_title="Song"), headers=tg(leader.telegram_id)
    )
    assert response.status_code == 200, response.text
    assert response.json()["start_time"] == "10:00:00" and response.json()["status"] == "confirmed"
    schedule = (await client.get("/webapp/api/schedule", headers=tg(leader.telegram_id))).json()
    assert [(item["band_name"], item["song_title"]) for item in schedule["bookings"]] == [("Rockers", "Song")]
    assert "10:00" not in schedule["free_slots"]["2026-10-15"] and "11:00" in schedule["free_slots"]["2026-10-15"]


async def test_personal_booking_without_a_band(client, session, make_user, freeze_now):
    freeze_now(NOW)
    user = await make_user()
    body = booking_body(None, purpose="personal")
    assert (await client.post("/webapp/api/bookings", json=body, headers=tg(user.telegram_id))).status_code == 200


async def test_booking_a_taken_slot_is_a_conflict(client, session, make_user, make_band, freeze_now):
    freeze_now(NOW)
    leader = await make_user()
    band = await make_band(leader, "Rockers")
    other = await make_user()
    rivals = await make_band(other, "Rivals")
    assert (await client.post("/webapp/api/bookings", json=booking_body(band.id), headers=tg(leader.telegram_id))).status_code == 200
    clash = await client.post("/webapp/api/bookings", json=booking_body(rivals.id, start="10:30", end="11:30"), headers=tg(other.telegram_id))
    assert clash.status_code == 409 and "not available" in clash.json()["detail"]


async def test_booking_for_a_foreign_band_is_forbidden(client, session, make_user, make_band, freeze_now):
    freeze_now(NOW)
    leader = await make_user()
    stranger = await make_user()
    band = await make_band(leader, "Rockers")
    response = await client.post("/webapp/api/bookings", json=booking_body(band.id), headers=tg(stranger.telegram_id))
    assert response.status_code == 403


async def test_booking_requires_a_fresh_signature_and_ignores_a_smuggled_telegram_id(client, session, make_user, make_band, freeze_now):
    freeze_now(NOW)
    leader = await make_user()
    stranger = await make_user()
    band = await make_band(leader, "Rockers")
    stale = {"X-Telegram-Init-Data": init_data(leader.telegram_id, age=2 * 60 * 60)}
    assert (await client.post("/webapp/api/bookings", json=booking_body(band.id), headers=stale)).status_code == 401
    assert (await client.post("/webapp/api/bookings", json=booking_body(band.id))).status_code == 401
    # the acting user always comes from the signature, never from the body
    spoofed = booking_body(band.id, telegram_id=leader.telegram_id)
    assert (await client.post("/webapp/api/bookings", json=spoofed, headers=tg(stranger.telegram_id))).status_code == 403


@pytest.mark.parametrize(
    "overrides",
    [
        {"purpose": "staff"},
        {"purpose": "personal"},  # band booking cannot be personal
        {"song_title": "x" * 151},
        {"start_time": "10:00", "end_time": "10:00"},
        {"booking_date": "not-a-date"},
    ],
)
async def test_invalid_booking_payloads_are_rejected(client, session, make_user, make_band, freeze_now, overrides):
    freeze_now(NOW)
    leader = await make_user()
    band = await make_band(leader, "Rockers")
    body = {**booking_body(band.id), **overrides}
    assert (await client.post("/webapp/api/bookings", json=body, headers=tg(leader.telegram_id))).status_code in (409, 422)


def _web_app_urls(markup) -> list[str]:
    return [button.web_app.url for row in markup.inline_keyboard for button in row if button.web_app]


def _callbacks(markup) -> set[str]:
    return {button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data}


def test_mini_app_url_puts_the_target_screen_and_join_code_in_the_query():
    from app.bot.keyboards import mini_app_url

    assert mini_app_url("https://club.example/") == "https://club.example/webapp/schedule"
    assert mini_app_url("https://club.example", "mine") == "https://club.example/webapp/schedule?screen=mine"
    assert mini_app_url("https://club.example", "bands", "ab12") == "https://club.example/webapp/schedule?screen=bands&join=ab12"


def test_rehearsals_and_bands_menus_open_the_mini_app():
    from app.bot import keyboards

    assert _web_app_urls(keyboards.rehearsals_menu_keyboard("https://club.example/")) == [
        "https://club.example/webapp/schedule",
        "https://club.example/webapp/schedule?screen=mine",
    ]
    assert _web_app_urls(keyboards.bands_menu_keyboard("https://club.example/")) == [
        "https://club.example/webapp/schedule?screen=bands"
    ]


def test_menus_without_a_public_url_only_offer_the_way_back():
    from app.bot import keyboards

    for markup in (keyboards.rehearsals_menu_keyboard(), keyboards.bands_menu_keyboard()):
        assert not _web_app_urls(markup)
        assert _callbacks(markup) == {"menu:back"}


def test_removed_bot_flows_leave_no_buttons_behind():
    from app.bot import keyboards

    removed = {"menu:booking", "user_bookings:0", "menu:bands", "menu:create_band", "menu:join_band"}
    for markup in (
        keyboards.rehearsals_menu_keyboard("https://club.example"),
        keyboards.bands_menu_keyboard("https://club.example"),
        keyboards.main_menu_inline(True),
        keyboards.admin_schedule_keyboard(),
        keyboards.admin_schedule_keyboard("https://club.example"),
        keyboards.admin_band_detail_keyboard(1),
    ):
        assert not removed & _callbacks(markup)
        assert not any(c.startswith(("admin_band_edit:", "admin_booking_edit:")) for c in _callbacks(markup))


def test_old_buttons_in_chat_history_are_routed_to_the_mini_app():
    from app.bot import handlers

    prefixes = handlers._legacy_prefixes()
    for stale in ("menu:booking", "booking:slot:3", "user_bookings:0", "user_booking_delete_confirm:5", "menu:bands",
                  "menu:create_band", "menu:join_band", "user_band:2:0", "user_band_member:1:2:role"):
        assert stale.startswith(prefixes), stale
    for live in ("menu:back", "menu:events", "admin_band:1:0", "admin_bands:0", "menu_section:bands", "plan:start"):
        assert not live.startswith(prefixes), live


def test_the_removed_flow_states_are_gone():
    from app.bot import states

    assert not hasattr(states, "BookingFlow") and not hasattr(states, "BandFlow")
    assert not hasattr(states.AdminFlow, "band_edit_value") and not hasattr(states.AdminFlow, "booking_edit_value")
    assert not hasattr(states, "DsvrFlow")
    for gone in ("day_toggle", "day_occupied_by", "weekly_booking_limit", "daily_booking_limit", "max_future_bookings",
                 "booking_window", "staff_booking_date", "staff_booking_time", "staff_booking_title", "group_admin_add"):
        assert not hasattr(states.AdminFlow, gone), gone
    assert hasattr(states.AdminFlow, "event_title") and hasattr(states.AdminFlow, "event_edit_value")  # concerts stay in the bot


# ---------- step 7a: my bookings from the Mini App ----------


async def book(session, user, band, day="2026-10-15", start=time(10), end=time(11)):
    from datetime import date

    return await bookings.create_booking(session, user.telegram_id, band.id, date.fromisoformat(day), start, end, "self", "Song")


async def test_my_bookings_lists_upcoming_ones_and_who_may_change_them(client, session, make_user, make_band, freeze_now):
    freeze_now(NOW)
    leader = await make_user()
    band = await make_band(leader, "Rockers")
    from app.services import bands as band_service

    member = await make_user()
    code = (await band_service.get_band_detail(session, band.id)).invite_code
    await band_service.join_band(session, member.telegram_id, code, "bass")
    await book(session, leader, band)
    rows = (await client.get("/webapp/api/my-bookings", headers=tg(leader.telegram_id))).json()["bookings"]
    assert [(row["band_name"], row["booking_date"], row["can_manage"]) for row in rows] == [("Rockers", "2026-10-15", True)]
    assert rows[0]["band_id"] == band.id
    seen_by_member = (await client.get("/webapp/api/my-bookings", headers=tg(member.telegram_id))).json()["bookings"]
    assert [row["can_manage"] for row in seen_by_member] == [False]  # a plain member sees it but cannot change it


async def test_my_bookings_need_an_approved_member(client, make_user):
    pending = await make_user(status="pending")
    assert (await client.get("/webapp/api/my-bookings")).status_code == 401
    assert (await client.get("/webapp/api/my-bookings", headers=tg(pending.telegram_id))).status_code == 403


async def test_leader_moves_a_booking_and_the_schedule_follows(client, session, make_user, make_band, freeze_now):
    freeze_now(NOW)
    leader = await make_user()
    band = await make_band(leader, "Rockers")
    booking = await book(session, leader, band)
    body = {"booking_date": "2026-10-16", "start_time": "12:00", "end_time": "13:30", "song_title": "New song"}
    response = await client.patch(f"/webapp/api/bookings/{booking.id}", json=body, headers=tg(leader.telegram_id))
    assert response.status_code == 200, response.text
    assert (response.json()["booking_date"], response.json()["start_time"], response.json()["end_time"]) == ("2026-10-16", "12:00:00", "13:30:00")
    free = (await client.get("/webapp/api/schedule")).json()["free_slots"]
    assert "10:00" in free["2026-10-15"] and "12:00" not in free["2026-10-16"]
    from app.services import schedule_history

    assert "updated" in [change["action"] for change in await schedule_history.list_changes(session, leader.telegram_id, band_id=band.id)]


async def test_moving_into_a_taken_slot_is_a_conflict_and_keeps_the_booking(client, session, make_user, make_band, freeze_now):
    freeze_now(NOW)
    leader = await make_user()
    rival = await make_user()
    band = await make_band(leader, "Rockers")
    rivals = await make_band(rival, "Rivals")
    mine = await book(session, leader, band)
    await book(session, rival, rivals, start=time(14), end=time(15))
    response = await client.patch(
        f"/webapp/api/bookings/{mine.id}", json={"start_time": "14:30", "end_time": "15:30"}, headers=tg(leader.telegram_id)
    )
    assert response.status_code == 409
    rows = (await client.get("/webapp/api/my-bookings", headers=tg(leader.telegram_id))).json()["bookings"]
    assert rows[0]["start_time"].startswith("10:00")


async def test_a_booking_can_slide_over_its_own_cells(client, session, make_user, make_band, freeze_now):
    freeze_now(NOW)
    leader = await make_user()
    band = await make_band(leader, "Rockers")
    booking = await book(session, leader, band)
    response = await client.patch(
        f"/webapp/api/bookings/{booking.id}", json={"start_time": "10:30", "end_time": "11:30"}, headers=tg(leader.telegram_id)
    )
    assert response.status_code == 200


async def test_only_those_who_manage_a_booking_may_change_or_cancel_it(client, session, make_user, make_band, freeze_now):
    freeze_now(NOW)
    leader = await make_user()
    stranger = await make_user()
    member = await make_user()
    band = await make_band(leader, "Rockers")
    from app.services import bands as band_service

    await band_service.join_band(session, member.telegram_id, (await band_service.get_band_detail(session, band.id)).invite_code, "bass")
    booking = await book(session, leader, band)
    url = f"/webapp/api/bookings/{booking.id}"
    for outsider in (stranger, member):
        assert (await client.patch(url, json={"song_title": "x"}, headers=tg(outsider.telegram_id))).status_code == 403
        assert (await client.delete(url, headers=tg(outsider.telegram_id))).status_code == 403
    assert (await client.patch(url, json={"song_title": "x"})).status_code == 401
    assert (await client.delete(url)).status_code == 401
    assert (await client.patch("/webapp/api/bookings/9999", json={"song_title": "x"}, headers=tg(leader.telegram_id))).status_code == 404


async def test_mutations_need_a_fresh_signature(client, session, make_user, make_band, freeze_now):
    freeze_now(NOW)
    leader = await make_user()
    band = await make_band(leader, "Rockers")
    booking = await book(session, leader, band)
    stale = {"X-Telegram-Init-Data": init_data(leader.telegram_id, age=2 * 60 * 60)}
    assert (await client.patch(f"/webapp/api/bookings/{booking.id}", json={"song_title": "x"}, headers=stale)).status_code == 401
    assert (await client.delete(f"/webapp/api/bookings/{booking.id}", headers=stale)).status_code == 401


async def test_leader_cancels_a_booking_softly_and_the_cell_frees_up(client, session, make_user, make_band, freeze_now):
    freeze_now(NOW)
    leader = await make_user()
    band = await make_band(leader, "Rockers")
    booking = await book(session, leader, band)
    response = await client.delete(f"/webapp/api/bookings/{booking.id}", headers=tg(leader.telegram_id))
    assert response.status_code == 200 and response.json()["id"] == booking.id
    assert (await client.get("/webapp/api/my-bookings", headers=tg(leader.telegram_id))).json()["bookings"] == []
    assert "10:00" in (await client.get("/webapp/api/schedule")).json()["free_slots"]["2026-10-15"]
    await session.refresh(booking)
    assert booking.status == "cancelled"  # soft-cancelled, the row stays for history
    assert (await client.delete(f"/webapp/api/bookings/{booking.id}", headers=tg(leader.telegram_id))).status_code == 409


def test_every_language_has_the_same_mini_app_strings():
    from app.bot.i18n import load_locale

    keys = {language: {key for key in load_locale(language) if key.startswith("webapp.")} for language in ("ru", "en", "kz")}
    assert keys["ru"] == keys["en"] == keys["kz"]


def test_admin_strings_exist_in_every_language():
    from app.bot.i18n import load_locale

    required = [
        "webapp.nav.admin",
        "webapp.admin.tab.requests",
        "webapp.admin.tab.users",
        "webapp.admin.tab.roles",
        *[f"webapp.admin.status.{status}" for status in ("pending", "approved", "rejected", "banned", "deleted")],
        *[f"webapp.admin.role.{role}" for role in ("user", "admin", "dsvr", "group_admin")],
    ]
    for language in ("ru", "en", "kz"):
        strings = load_locale(language)
        assert [key for key in required if not strings.get(key)] == [], language


def test_app_js_only_uses_defined_admin_strings():
    import re
    from pathlib import Path

    from app.bot.i18n import load_locale

    source = (Path(__file__).resolve().parent.parent / "app" / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    keys = set(re.findall(r'\bt\("(webapp\.admin\.[A-Za-z_.]+)"', source))
    keys |= set(re.findall(r'\bt\(`(webapp\.admin\.[A-Za-z_.]+)`', source))
    keys |= set(re.findall(r'"(webapp\.admin\.[A-Za-z_.]+)"', source))
    assert keys, "the admin screen must use webapp.admin.* strings"
    ru = load_locale("ru")
    assert sorted(key for key in keys if key not in ru) == []


ADMIN_BASE = "https://club.example/"
ADMIN_REQUESTS_URL = "https://club.example/webapp/schedule?screen=admin&tab=requests"
ADMIN_ROLES_URL = "https://club.example/webapp/schedule?screen=admin&tab=roles"
ADMIN_SETTINGS_URL = "https://club.example/webapp/schedule?screen=admin&tab=settings"
ADMIN_HISTORY_URL = "https://club.example/webapp/schedule?screen=admin&tab=history"
SCHEDULE_URL = "https://club.example/webapp/schedule"


def test_mini_app_url_can_target_an_admin_tab():
    from app.bot.keyboards import mini_app_url

    assert mini_app_url("https://club.example", "admin", tab="requests") == ADMIN_REQUESTS_URL
    assert mini_app_url("https://club.example", "mine") == "https://club.example/webapp/schedule?screen=mine"


def test_admin_menus_open_the_mini_app():
    from app.bot import keyboards

    admin_urls = _web_app_urls(keyboards.admin_inline_keyboard(ADMIN_BASE))
    assert ADMIN_REQUESTS_URL in admin_urls and ADMIN_ROLES_URL in admin_urls
    assert ADMIN_SETTINGS_URL in admin_urls
    group_admin_urls = _web_app_urls(keyboards.staff_inline_keyboard(True, False, ADMIN_BASE))
    assert ADMIN_REQUESTS_URL in group_admin_urls and ADMIN_ROLES_URL not in group_admin_urls
    assert ADMIN_HISTORY_URL in group_admin_urls and SCHEDULE_URL not in group_admin_urls
    dsvr_urls = _web_app_urls(keyboards.staff_inline_keyboard(False, True, ADMIN_BASE))
    assert SCHEDULE_URL in dsvr_urls and ADMIN_HISTORY_URL in dsvr_urls  # take the room on the grid, read the history
    assert [url for url in dsvr_urls if "screen=admin" in url and "tab=history" not in url] == []


def test_admin_screens_open_the_mini_app_for_what_moved_out_of_the_bot():
    from app.bot import keyboards

    assert {SCHEDULE_URL, ADMIN_SETTINGS_URL} <= set(_web_app_urls(keyboards.admin_schedule_keyboard(ADMIN_BASE)))
    assert ADMIN_HISTORY_URL in _web_app_urls(keyboards.admin_actions_keyboard(ADMIN_BASE))
    # without a public URL no dead web_app buttons are drawn, and the journal stays reachable
    assert _web_app_urls(keyboards.admin_actions_keyboard()) == []
    assert "admin:action_history" in _callbacks(keyboards.admin_actions_keyboard())
    assert _web_app_urls(keyboards.admin_schedule_keyboard()) == []
    assert _web_app_urls(keyboards.staff_inline_keyboard(True, True)) == []


def test_admin_menus_drop_the_moderation_callbacks():
    from app.bot import keyboards

    for markup in (
        keyboards.admin_inline_keyboard(ADMIN_BASE),
        keyboards.admin_inline_keyboard(),
        keyboards.staff_inline_keyboard(True, False, ADMIN_BASE),
        keyboards.staff_inline_keyboard(True, False),
    ):
        callbacks = _callbacks(markup)
        assert "admin_section:users" not in callbacks and "admin_section:admins" not in callbacks
        assert "menu:back" in callbacks
    assert "admin_section:schedule" in _callbacks(keyboards.admin_inline_keyboard())
    assert "groups:list" not in _callbacks(keyboards.admin_inline_keyboard())  # groups moved to the Mini App roles tab
    assert _web_app_urls(keyboards.admin_inline_keyboard()) == []  # no public URL: no dead web_app buttons


def test_stale_moderation_buttons_are_routed_to_the_legacy_handler():
    from app.bot.handlers import LEGACY_MINI_APP_SCREENS

    prefixes, _button, _text = LEGACY_MINI_APP_SCREENS["admin"]
    for data in (
        "admin_section:users",
        "admin_section:admins",
        "admin_users:pending:0",
        "admin_users:manual",
        "admin_user:5:pending:0",
        "admin_user_action:ban:5",
        "admin:pending_users",
        "admin:approve_user",
        "admin:reject_user",
        "admin:ban_user",
        "admin:delete_user",
        "admin:add_admin",
        "admin:remove_admin",
        # step 11.6: settings, taking the room, cancelling, groups, DSVR and the schedule history
        "admin_section:settings",
        "admin:enable_day",
        "admin:disable_day",
        "admin:weekly_booking_limit",
        "admin:daily_booking_limit",
        "admin:max_future_bookings",
        "admin:personal_bookings_toggle",
        "admin:booking_window",
        "admin:staff_booking",
        "admin_bookings_manage:0",
        "admin_bookings_delete:2",
        "admin_booking_manage:7",
        "admin_booking_delete:7",
        "admin_booking_delete_confirm:7",
        "groups:list",
        "groups:open:1",
        "groups:add:1",
        "groups:remove:1:5",
        "dsvr:start",
        "dsvr:date:2026-10-20",
        "dsvr:confirm",
        "dsvr:grant",
        "sched_history:all",
        "band_history:3:a",
    ):
        assert data.startswith(tuple(prefixes)), data
    for data in (
        "admin_section:schedule", "admin_section:actions", "admin_section:bands", "admin_section:events", "admin_section:guide",
        "admin:back", "admin:action_history", "admin:create_event", "registration:approve:5", "plan:admin",
        "admin_band:1:0", "admin_bands:0", "admin_event:3",
    ):
        assert not data.startswith(tuple(prefixes)), data


def test_removed_admin_reply_buttons_are_routed_to_the_mini_app():
    from app.bot.handlers import REMOVED_ADMIN_TEXTS

    for text in ("Включить день", "Выключить день", "Лимит в неделю", "Лимит в день", "Часы кабинета", "Занять время", "Удалить бронь"):
        assert text in REMOVED_ADMIN_TEXTS, text
    assert "История действий" not in REMOVED_ADMIN_TEXTS and "Создать концерт" not in REMOVED_ADMIN_TEXTS


def test_the_removed_feature_routers_are_gone():
    import importlib.util

    for name in ("handlers_dsvr", "handlers_groups", "handlers_history"):
        assert importlib.util.find_spec(f"app.bot.{name}") is None, name
    from app.bot import api_client, main

    assert not hasattr(main, "dsvr_router") and not hasattr(main, "groups_router") and not hasattr(main, "history_router")
    for method in ("priority_preview", "create_priority_booking", "set_dsvr", "schedule_history", "update_day", "create_staff_booking"):
        assert not hasattr(api_client.BackendClient, method), method


# ---------- step 11.1: change any booking from its card on the grid ----------


async def test_schedule_says_who_may_change_each_booking(client, session, make_user, make_band, freeze_now):
    from sqlalchemy import select

    from app.db import models

    leader = await booked_schedule(session, make_user, make_band, freeze_now)
    admin = await make_user(role="admin")
    dsvr = await make_user(role="dsvr")
    stranger = await make_user()
    group_admin = await make_user()
    band_group = (await session.execute(select(models.Band.group_id))).scalar_one()
    session.add(models.GroupAdmin(group_id=band_group, user_id=group_admin.id))
    await session.commit()

    async def can_manage(user):
        payload = (await client.get("/webapp/api/schedule", headers=tg(user.telegram_id))).json()
        return payload["bookings"][0]["can_manage"]

    assert [await can_manage(user) for user in (leader, admin, dsvr, group_admin, stranger)] == [True, True, True, True, False]
    anonymous = (await client.get("/webapp/api/schedule")).json()["bookings"][0]
    assert "can_manage" not in anonymous
    assert anonymous["band_id"] is not None


async def test_admin_moves_and_cancels_somebody_elses_booking_past_the_limits(client, session, make_user, make_band, freeze_now):
    freeze_now(NOW)
    leader = await make_user()
    admin = await make_user(role="admin")
    band = await make_band(leader, "Rockers")
    booking = await book(session, leader, band)
    body = {"booking_date": "2026-10-16", "start_time": "10:00", "end_time": "18:00"}  # 8 h, over the weekly limit of 4 h
    moved = await client.patch(f"/webapp/api/bookings/{booking.id}", json=body, headers=tg(admin.telegram_id))
    assert moved.status_code == 200, moved.text
    assert moved.json()["end_time"] == "18:00:00"
    cancelled = await client.delete(f"/webapp/api/bookings/{booking.id}", headers=tg(admin.telegram_id))
    assert cancelled.status_code == 200
    await session.refresh(booking)
    assert booking.status == "cancelled"


# ---------- step 11.2: DSVR takeover from the grid ----------


PRIORITY = {"booking_date": "2026-10-14", "start_time": "15:30", "end_time": "16:30"}


async def test_only_dsvr_and_admins_may_preview_or_take_over(client, session, make_user, freeze_now):
    freeze_now(NOW)
    member = await make_user()
    preview = await client.get("/webapp/api/priority/preview", params=PRIORITY, headers=tg(member.telegram_id))
    assert preview.status_code == 403
    created = await client.post("/webapp/api/priority", json={**PRIORITY, "title": "Ректор"}, headers=tg(member.telegram_id))
    assert created.status_code == 403
    assert (await client.get("/webapp/api/priority/preview", params=PRIORITY)).status_code == 401


async def test_dsvr_previews_and_takes_over_through_the_mini_app(client, session, make_user, make_band, freeze_now, published):
    leader = await booked_schedule(session, make_user, make_band, freeze_now)  # Rockers, 15:00-16:00
    dsvr = await make_user(role="dsvr", group_slug=None)
    admin = await make_user(role="admin")
    headers = tg(dsvr.telegram_id)

    preview = (await client.get("/webapp/api/priority/preview", params=PRIORITY, headers=headers)).json()
    assert [row["band_name"] for row in preview["bookings"]] == ["Rockers"]
    assert len(await bookings.list_schedule(session)) == 1  # a preview changes nothing

    published.clear()
    created = await client.post("/webapp/api/priority", json={**PRIORITY, "title": "Ректор"}, headers=headers)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["booking"]["purpose"] == "staff" and body["booking"]["start_time"] == "15:30:00"
    assert [row["band_name"] for row in body["cancelled"]] == ["Rockers"]
    assert any(event["type"] == "notify" and event["telegram_id"] == leader.telegram_id for event in published)

    taken = (await client.get("/webapp/api/schedule")).json()["bookings"]
    assert [(row["purpose"], row["start_time"]) for row in taken] == [("staff", "15:30:00")]

    admin_preview = await client.get("/webapp/api/priority/preview", params=PRIORITY, headers=tg(admin.telegram_id))
    assert admin_preview.status_code == 200


async def test_takeover_rejects_bad_title_and_time(client, session, make_user, freeze_now):
    freeze_now(NOW)
    dsvr = await make_user(role="dsvr", group_slug=None)
    headers = tg(dsvr.telegram_id)
    assert (await client.post("/webapp/api/priority", json={**PRIORITY, "title": ""}, headers=headers)).status_code == 422
    assert (await client.post("/webapp/api/priority", json={**PRIORITY, "title": "x" * 151}, headers=headers)).status_code == 422
    past = {**PRIORITY, "start_time": "09:00", "end_time": "10:00", "title": "Ранний"}  # NOW is 12:00
    assert (await client.post("/webapp/api/priority", json=past, headers=headers)).status_code == 409
    odd = {**PRIORITY, "end_time": "16:10", "title": "Кривое"}
    assert (await client.post("/webapp/api/priority", json=odd, headers=headers)).status_code == 409


# ---------- step 11.3: an admin occupies time through the same mode ----------


async def test_admin_takeover_is_recorded_as_staff_and_dsvr_as_dsvr(client, session, make_user, freeze_now):
    from sqlalchemy import select

    from app.db.models import AuditLog, Booking, ScheduleChange

    freeze_now(NOW)
    admin = await make_user(role="admin")
    dsvr = await make_user(role="dsvr", group_slug=None)
    first = await client.post("/webapp/api/priority", json={**PRIORITY, "title": "Ремонт"}, headers=tg(admin.telegram_id))
    assert first.status_code == 200, first.text
    later = {"booking_date": "2026-10-14", "start_time": "17:00", "end_time": "18:00", "title": "Ректор"}
    second = await client.post("/webapp/api/priority", json=later, headers=tg(dsvr.telegram_id))
    assert second.status_code == 200, second.text

    sources = {row.id: row.source for row in (await session.execute(select(Booking))).scalars()}
    assert sources[first.json()["booking"]["id"]] == "staff"
    assert sources[second.json()["booking"]["id"]] == "dsvr"

    actions = {row.action for row in (await session.execute(select(AuditLog))).scalars()}
    assert {"booking.staff_create", "booking.priority_create"} <= actions
    reasons = [row.reason for row in (await session.execute(select(ScheduleChange))).scalars()]
    assert any(reason and reason.startswith("Админ занял время") for reason in reasons)
    assert any(reason and reason.startswith("Приоритет ДСВР") for reason in reasons)
