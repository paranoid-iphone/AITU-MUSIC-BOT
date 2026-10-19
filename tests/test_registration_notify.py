"""How admins hear about a new registration request: chat buttons, a Mini App link, or nothing."""
from types import SimpleNamespace

import pytest

from app.bot import handlers
from app.core.config import settings as app_settings
from tests.test_webapp import client, tg  # noqa: F401  (`client` is a fixture)

SETTINGS = "/webapp/api/admin/settings"
AUTH = {"X-Internal-Token": app_settings.internal_api_token}


async def test_the_mode_defaults_to_chat_and_can_be_changed_by_the_main_admin(client, make_user):
    admin = await make_user(role="admin", group_slug=None)
    headers = tg(admin.telegram_id)

    assert (await client.get(SETTINGS, headers=headers)).json()["registration_notify"] == "chat"
    assert (await client.get("/admin/settings/registration-notify", headers=AUTH)).json() == {"mode": "chat"}

    saved = await client.put(f"{SETTINGS}/registration-notify", json={"mode": "mini_app"}, headers=headers)
    assert saved.status_code == 200 and saved.json() == {"registration_notify": "mini_app"}
    assert (await client.get(SETTINGS, headers=headers)).json()["registration_notify"] == "mini_app"
    assert (await client.get("/admin/settings/registration-notify", headers=AUTH)).json() == {"mode": "mini_app"}


async def test_only_known_modes_and_only_the_main_admin(client, make_user):
    admin = await make_user(role="admin", group_slug=None)
    plain = await make_user()

    bad = await client.put(f"{SETTINGS}/registration-notify", json={"mode": "email"}, headers=tg(admin.telegram_id))
    forbidden = await client.put(f"{SETTINGS}/registration-notify", json={"mode": "off"}, headers=tg(plain.telegram_id))

    assert bad.status_code == 422 and forbidden.status_code == 403
    assert (await client.get(SETTINGS, headers=tg(admin.telegram_id))).json()["registration_notify"] == "chat"


async def test_changing_the_mode_is_written_to_the_action_journal(client, session, make_user):
    from sqlalchemy import select

    from app.db import models

    admin = await make_user(role="admin", group_slug=None)
    await client.put(f"{SETTINGS}/registration-notify", json={"mode": "off"}, headers=tg(admin.telegram_id))

    actions = (await session.execute(select(models.AuditLog.action))).scalars().all()
    assert "settings.registration_notify.update" in actions


# ---- the bot side ----

PENDING = {
    "telegram_id": 77,
    "first_name": "Иван",
    "last_name": "Петров",
    "study_group": "SE-2401",
    "telegram_username": "ivan",
    "group_id": 1,
}


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=len(self.sent))


@pytest.fixture
def bot_env(monkeypatch):
    """The bot module with a fake backend client: `env.mode` is what the club setting says (or an exception)."""
    env = SimpleNamespace(mode="chat", bot=FakeBot())

    async def notify_mode():
        if isinstance(env.mode, Exception):
            raise env.mode
        return env.mode

    async def reviewers(group_id=None):
        return {500}

    async def groups():
        return [{"id": 1, "title": "Music"}]

    async def no_messages(telegram_id):
        return []

    async def remember(telegram_id, chat_id, message_id):
        return None

    async def base_url():
        return "https://club.example"

    fake_api = SimpleNamespace(registration_notify_mode=notify_mode, list_groups=groups)
    monkeypatch.setattr(handlers, "api", fake_api)
    monkeypatch.setattr(handlers, "get_registration_admin_ids", reviewers)
    monkeypatch.setattr(handlers, "get_registration_notice_messages", no_messages)
    monkeypatch.setattr(handlers, "remember_registration_notice_message", remember)
    monkeypatch.setattr(handlers, "get_webapp_base_url", base_url)
    handlers.registration_notice_sent.clear()
    env.message = SimpleNamespace(bot=env.bot)
    yield env
    handlers.registration_notice_sent.clear()


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


async def test_chat_mode_sends_the_approve_and_reject_buttons(bot_env):
    assert await handlers.notify_admins_about_registration(bot_env.message, PENDING) is True

    [sent] = bot_env.bot.sent
    assert sent["chat_id"] == 500 and "Одобрить регистрацию?" in sent["text"]
    assert [button.callback_data for button in _buttons(sent["reply_markup"])] == ["registration:approve:77", "registration:reject:77"]


async def test_mini_app_mode_sends_a_link_to_the_requests_tab(bot_env):
    bot_env.mode = "mini_app"

    assert await handlers.notify_admins_about_registration(bot_env.message, PENDING) is True

    [sent] = bot_env.bot.sent
    assert "Одобрить регистрацию?" not in sent["text"] and "Иван" in sent["text"]
    [button] = _buttons(sent["reply_markup"])
    assert button.callback_data is None
    assert button.web_app.url == "https://club.example/webapp/schedule?screen=admin&tab=requests"


async def test_off_mode_sends_nothing(bot_env):
    bot_env.mode = "off"

    assert await handlers.notify_admins_about_registration(bot_env.message, PENDING) is False
    assert bot_env.bot.sent == []


async def test_an_unreachable_backend_falls_back_to_the_chat_buttons(bot_env):
    bot_env.mode = RuntimeError("backend is down")

    assert await handlers.notify_admins_about_registration(bot_env.message, PENDING) is True
    [sent] = bot_env.bot.sent
    assert _buttons(sent["reply_markup"])[0].callback_data == "registration:approve:77"
