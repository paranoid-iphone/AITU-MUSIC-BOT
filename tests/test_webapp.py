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
