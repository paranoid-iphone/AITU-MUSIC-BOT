import asyncio
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter
from aiogram.methods import EditMessageMedia
from aiogram.types import Message

from app.bot import handlers
from app.bot.menu_refresh import MenuRefresher
from app.services import bands

METHOD = EditMessageMedia.model_construct()


class FakeRedis:
    def __init__(self, stored: dict[str, str]):
        self.stored = dict(stored)

    async def hgetall(self, key):
        return dict(self.stored)

    async def hdel(self, key, field):
        self.stored.pop(field, None)

    async def set(self, *args, **kwargs):
        return None


class FakeBot:
    """Replays a scripted outcome per edit_message_media call; an exception instance is raised, anything else returned."""

    def __init__(self, script: dict[int, list]):
        self.script = {chat_id: list(outcomes) for chat_id, outcomes in script.items()}
        self.calls: list[int] = []

    async def edit_message_media(self, chat_id, message_id, media, reply_markup):
        self.calls.append(chat_id)
        outcomes = self.script.get(chat_id)
        outcome = outcomes.pop(0) if outcomes else True
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def menus(monkeypatch):
    """Three stored menus, a recorded sleep and a stubbed schedule image."""
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async def fake_media():
        return "file-id", "fingerprint", True

    redis = FakeRedis({"1": "10:0", "2": "20:1", "3": "30:0"})
    monkeypatch.setattr(handlers, "redis", redis)
    monkeypatch.setattr(handlers, "get_schedule_image_media", fake_media)
    monkeypatch.setattr(handlers.asyncio, "sleep", fake_sleep)
    return SimpleNamespace(redis=redis, sleeps=sleeps)


def _retry_after(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(METHOD, "Too Many Requests", seconds)


def _bad_request(text: str) -> TelegramBadRequest:
    return TelegramBadRequest(METHOD, text)


async def test_refresh_pauses_between_chats(menus):
    bot = FakeBot({})
    await handlers.refresh_all_schedule_menus(bot)

    assert bot.calls == [1, 2, 3]
    assert menus.sleeps == [handlers.MENU_REFRESH_PAUSE] * 2  # between chats, not before the first one


async def test_refresh_waits_out_a_flood_limit_and_retries_the_same_chat(menus):
    bot = FakeBot({2: [_retry_after(7)]})
    await handlers.refresh_all_schedule_menus(bot)

    assert bot.calls == [1, 2, 2, 3]
    assert 7 in menus.sleeps


async def test_refresh_skips_a_chat_that_stays_rate_limited_and_keeps_going(menus):
    bot = FakeBot({2: [_retry_after(3), _retry_after(3)]})
    await handlers.refresh_all_schedule_menus(bot)

    assert bot.calls == [1, 2, 2, 3]
    assert "2" in menus.redis.stored  # skipped, not forgotten


async def test_refresh_aborts_on_a_very_long_flood_wait(menus):
    bot = FakeBot({1: [_retry_after(handlers.MENU_REFRESH_MAX_WAIT + 1)]})
    await handlers.refresh_all_schedule_menus(bot)

    assert bot.calls == [1]
    assert handlers.MENU_REFRESH_MAX_WAIT + 1 not in menus.sleeps
    assert set(menus.redis.stored) == {"1", "2", "3"}


@pytest.mark.parametrize(
    "error",
    [
        TelegramForbiddenError(METHOD, "Forbidden: bot was blocked by the user"),
        TelegramNotFound(METHOD, "Not Found"),
        _bad_request("Bad Request: message to edit not found"),
        _bad_request("Bad Request: chat not found"),
    ],
)
async def test_refresh_forgets_menus_that_are_gone(menus, error):
    bot = FakeBot({2: [error]})
    await handlers.refresh_all_schedule_menus(bot)

    assert bot.calls == [1, 2, 3]
    assert set(menus.redis.stored) == {"1", "3"}


@pytest.mark.parametrize(
    "error",
    [
        _bad_request("Bad Request: message is not modified: specified new message content is the same"),
        _bad_request("Bad Request: wrong file identifier/HTTP URL specified"),
        RuntimeError("network down"),
    ],
)
async def test_refresh_keeps_menus_after_harmless_errors(menus, error):
    bot = FakeBot({2: [error]})
    await handlers.refresh_all_schedule_menus(bot)

    assert bot.calls == [1, 2, 3]
    assert set(menus.redis.stored) == {"1", "2", "3"}


async def test_refresh_remembers_the_file_id_of_a_freshly_uploaded_image(menus, monkeypatch):
    remembered: list[tuple[str, str]] = []

    async def fake_media():
        return "raw-image", "fp", False

    async def fake_remember(fingerprint, message):
        remembered.append((fingerprint, message.photo[-1].file_id))

    sent = Message.model_construct(photo=[SimpleNamespace(file_id="uploaded-id")])
    media_used: list = []

    class Bot(FakeBot):
        async def edit_message_media(self, chat_id, message_id, media, reply_markup):
            media_used.append(media.media)
            return sent

    monkeypatch.setattr(handlers, "get_schedule_image_media", fake_media)
    monkeypatch.setattr(handlers, "remember_schedule_file_id", fake_remember)
    await handlers.refresh_all_schedule_menus(Bot({}))

    assert remembered == [("fp", "uploaded-id")]
    assert media_used == ["raw-image", "uploaded-id", "uploaded-id"]  # later chats reuse the uploaded file


# ---------- MenuRefresher ----------


async def test_refresher_does_nothing_until_something_changed():
    runs: list[int] = []

    async def refresh():
        runs.append(1)

    refresher = MenuRefresher(refresh)
    refresher.poll()
    await asyncio.sleep(0)
    assert runs == []


async def test_refresher_coalesces_a_burst_into_one_pass():
    runs: list[int] = []

    async def refresh():
        runs.append(1)

    refresher = MenuRefresher(refresh)
    for _ in range(5):
        refresher.mark_dirty()
    refresher.poll()
    refresher.poll()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert runs == [1]


async def test_refresher_reruns_once_for_a_change_that_arrives_mid_pass():
    runs: list[int] = []
    release = asyncio.Event()

    async def refresh():
        runs.append(1)
        await release.wait()

    refresher = MenuRefresher(refresh)
    refresher.mark_dirty()
    refresher.poll()
    await asyncio.sleep(0)
    assert runs == [1]

    refresher.mark_dirty()
    refresher.mark_dirty()
    refresher.poll()  # a pass is still running: no second one starts
    await asyncio.sleep(0)
    assert runs == [1]

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    refresher.poll()
    await asyncio.sleep(0)
    assert runs == [1, 1]
    refresher.poll()
    await asyncio.sleep(0)
    assert runs == [1, 1]  # the flag was consumed by the single follow-up


async def test_a_failed_pass_does_not_block_the_next_one():
    runs: list[int] = []

    async def refresh():
        runs.append(1)
        if len(runs) == 1:
            raise RuntimeError("backend down")

    refresher = MenuRefresher(refresh)
    refresher.mark_dirty()
    refresher.poll()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    refresher.mark_dirty()
    refresher.poll()
    await asyncio.sleep(0)
    assert runs == [1, 1]


async def test_close_cancels_a_running_pass():
    started = asyncio.Event()
    cancelled: list[bool] = []

    async def refresh():
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    refresher = MenuRefresher(refresh)
    refresher.mark_dirty()
    refresher.poll()
    await started.wait()
    refresher.close()
    await asyncio.sleep(0)
    assert cancelled == [True]


# ---------- the services queue the refresh themselves ----------


async def test_changing_the_responsible_refreshes_schedule_menus(session, make_user, make_band, published):
    owner = await make_user()
    member_user = await make_user()
    band = await make_band(owner)
    await bands.join_band(session, member_user.telegram_id, band.invite_code, "гитара")
    detail = await bands.get_band_detail(session, band.id)
    member = next(m for m in detail.members if m.user_id == member_user.id)
    published.clear()

    await bands.update_band_responsible(session, band.id, owner.telegram_id, member.id)

    assert published == [{"type": "schedule_changed"}]


async def test_removing_the_responsible_member_refreshes_schedule_menus(session, make_user, make_band, published):
    owner = await make_user()
    member_user = await make_user()
    band = await make_band(owner)
    await bands.join_band(session, member_user.telegram_id, band.invite_code, "гитара")
    detail = await bands.get_band_detail(session, band.id)
    member = next(m for m in detail.members if m.user_id == member_user.id)
    await bands.update_band_responsible(session, band.id, owner.telegram_id, member.id)
    published.clear()

    await bands.remove_band_member(session, band.id, member.id, owner.telegram_id)

    assert published == [{"type": "schedule_changed"}]


async def test_removing_an_ordinary_member_does_not_refresh_schedule_menus(session, make_user, make_band, published):
    owner = await make_user()
    member_user = await make_user()
    band = await make_band(owner)
    await bands.join_band(session, member_user.telegram_id, band.invite_code, "гитара")
    detail = await bands.get_band_detail(session, band.id)
    member = next(m for m in detail.members if m.user_id == member_user.id)
    published.clear()

    await bands.remove_band_member(session, band.id, member.id, owner.telegram_id)

    assert published == []
