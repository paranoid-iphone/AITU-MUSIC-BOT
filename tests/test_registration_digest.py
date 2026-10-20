"""The summary of waiting registration requests that the bot sends to moderators when it starts."""
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramAPIError

from app.bot import registration_digest
from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.services import users

AUTH = {"X-Internal-Token": settings.internal_api_token}


@pytest_asyncio.fixture
async def client(session):
    async def _session_override():
        yield session

    app.dependency_overrides[get_session] = _session_override
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def test_each_moderator_counts_the_requests_of_the_groups_they_moderate(session, make_user, grant, role_holder):
    admin = await make_user(group_slug=None)
    await grant(admin, "Админ")
    dance_admin = await make_user(group_slug="dance")
    await grant(dance_admin, "Админ группы", "dance")
    bands_only = await role_holder("Коллективы", ["bands.manage"])
    for slug in ("dance", "dance", "music"):
        await make_user(status="pending", group_slug=slug)
    await make_user(status="banned", group_slug="dance")
    await make_user(status="rejected", group_slug="music")

    counts = await users.pending_registration_counts(session)

    assert counts[admin.telegram_id] == 3
    assert counts[dance_admin.telegram_id] == 2
    assert bands_only.telegram_id not in counts


async def test_nobody_is_listed_when_no_request_waits(session, make_user, grant):
    admin = await make_user(group_slug=None)
    await grant(admin, "Админ")
    await make_user(group_slug="dance")  # approved

    assert await users.pending_registration_counts(session) == {}


async def test_moderators_who_are_not_approved_hear_nothing(session, make_user, grant):
    banned = await make_user(status="banned", group_slug=None)
    await grant(banned, "Админ")
    await make_user(status="pending", group_slug="music")

    assert await users.pending_registration_counts(session) == {}


async def test_the_digest_route_is_internal_and_lists_counts(client, session, make_user, grant):
    admin = await make_user(group_slug=None)
    await grant(admin, "Админ")
    await make_user(status="pending", group_slug="music")

    assert (await client.get("/users/registration-digest")).status_code in (401, 403)
    reply = await client.get("/users/registration-digest", headers=AUTH)

    assert reply.status_code == 200
    assert reply.json() == [{"telegram_id": admin.telegram_id, "pending": 1}]


# --- the bot side -------------------------------------------------------------------------------------------------


class FakeBot:
    def __init__(self, failing=()):
        self.sent = []
        self.failing = set(failing)

    async def send_message(self, chat_id, text, reply_markup=None):
        if chat_id in self.failing:
            raise TelegramAPIError(method=None, message="chat not found")
        self.sent.append((chat_id, text, reply_markup))


def _api(digest):
    async def registration_digest():
        return digest

    return SimpleNamespace(registration_digest=registration_digest)


def _urls(markup):
    return [button.web_app.url for row in markup.inline_keyboard for button in row if button.web_app]


async def test_every_moderator_gets_one_message_with_a_link_to_the_requests():
    bot = FakeBot()

    sent = await registration_digest.send_registration_digest(
        bot, _api([{"telegram_id": 1, "pending": 3}, {"telegram_id": 2, "pending": 1}]), "chat", "https://club.example"
    )

    assert sent == 2
    assert [(chat, "3" in text) for chat, text, _ in bot.sent] == [(1, True), (2, False)]
    assert _urls(bot.sent[0][2]) == ["https://club.example/webapp/schedule?screen=admin&tab=requests"]


async def test_the_digest_is_silent_when_the_club_turned_registration_notices_off():
    bot = FakeBot()

    assert await registration_digest.send_registration_digest(bot, _api([{"telegram_id": 1, "pending": 3}]), "off", "https://x") == 0
    assert bot.sent == []


async def test_without_a_public_url_the_message_goes_out_without_a_button():
    bot = FakeBot()

    await registration_digest.send_registration_digest(bot, _api([{"telegram_id": 1, "pending": 2}]), "mini_app", None)

    assert len(bot.sent) == 1 and bot.sent[0][2] is None


async def test_one_unreachable_moderator_does_not_stop_the_rest():
    bot = FakeBot(failing={1})

    sent = await registration_digest.send_registration_digest(
        bot, _api([{"telegram_id": 1, "pending": 1}, {"telegram_id": 2, "pending": 1}]), "chat", "https://x"
    )

    assert sent == 1 and [chat for chat, _, _ in bot.sent] == [2]


async def test_an_empty_digest_sends_nothing():
    bot = FakeBot()

    assert await registration_digest.send_registration_digest(bot, _api([]), "chat", "https://x") == 0
    assert bot.sent == []


async def test_the_start_up_digest_waits_for_a_backend_that_is_still_starting(monkeypatch):
    calls = {"count": 0}

    async def flaky_digest():
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("backend is starting")
        return [{"telegram_id": 1, "pending": 2}]

    async def notify_mode():
        return "chat"

    async def base_url():
        return "https://club.example"

    monkeypatch.setattr(registration_digest, "get_webapp_base_url", base_url)
    bot = FakeBot()

    await registration_digest.send_registration_digest_on_start(
        bot, SimpleNamespace(registration_digest=flaky_digest, registration_notify_mode=notify_mode), attempts=3, delay=0
    )

    assert calls["count"] == 2 and [chat for chat, _, _ in bot.sent] == [1]
