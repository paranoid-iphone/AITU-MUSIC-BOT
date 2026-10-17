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


def test_rehearsals_menu_leads_with_the_mini_app_booking_button():
    from app.bot import keyboards

    rows = keyboards.rehearsals_menu_keyboard("https://club.example/").inline_keyboard
    assert rows[0][0].web_app.url == "https://club.example/webapp/schedule"
    assert any(button.callback_data == "menu:booking" for row in rows for button in row)  # the old flow stays
    assert all(button.web_app is None for row in keyboards.rehearsals_menu_keyboard().inline_keyboard for button in row)
