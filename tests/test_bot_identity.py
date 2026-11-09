from app.bot import handlers
from app.core import bot_identity
from app.core.config import settings


def _fresh(monkeypatch, fetch):
    monkeypatch.setattr(bot_identity, "_state", bot_identity._State())
    monkeypatch.setattr(settings, "bot_token", "123:abc")
    monkeypatch.setattr(bot_identity, "_fetch_username", fetch)


async def test_the_username_is_asked_once_and_kept(monkeypatch):
    calls = []

    async def fetch():
        calls.append(1)
        return "aitu_music_bot"

    _fresh(monkeypatch, fetch)
    assert await bot_identity.bot_username() == "aitu_music_bot"
    assert await bot_identity.bot_username() == "aitu_music_bot"
    assert len(calls) == 1


async def test_a_failure_hides_the_links_and_is_retried_after_a_pause(monkeypatch):
    calls = []

    async def fetch():
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("telegram is down")
        return "aitu_music_bot"

    _fresh(monkeypatch, fetch)
    clock = [1000.0]
    monkeypatch.setattr(bot_identity.time, "monotonic", lambda: clock[0])
    assert await bot_identity.bot_username() is None
    assert await bot_identity.bot_username() is None  # within the pause: Telegram is not asked again
    assert len(calls) == 1
    clock[0] += bot_identity.RETRY_SECONDS + 1
    assert await bot_identity.bot_username() == "aitu_music_bot"


async def test_without_a_token_telegram_is_not_asked(monkeypatch):
    async def fetch():
        raise AssertionError("must not be called")

    _fresh(monkeypatch, fetch)
    monkeypatch.setattr(settings, "bot_token", "")
    assert await bot_identity.bot_username() is None


def test_invite_link_opens_the_bot_with_the_join_payload():
    assert bot_identity.invite_link("aitu_music_bot", "AB12CD34") == "https://t.me/aitu_music_bot?start=join_AB12CD34"
    assert bot_identity.invite_link(None, "AB12CD34") is None
    assert bot_identity.invite_link("aitu_music_bot", None) is None


def test_the_bot_reads_the_code_back_from_the_link(monkeypatch):
    link = bot_identity.invite_link("aitu_music_bot", "AB12CD34")
    payload = link.split("?start=", 1)[1]
    message = type("Message", (), {"text": f"/start {payload}"})()
    assert handlers.extract_join_code_from_start(message) == "AB12CD34"


def test_the_admin_band_card_shows_the_link_next_to_the_code():
    band = {"id": 7, "name": "Echo", "group": None, "responsible": None, "invite_code": "AB12CD34", "members": []}
    card = handlers.format_band_card(band, "aitu_music_bot")
    assert "Код: AB12CD34" in card
    assert "Ссылка: https://t.me/aitu_music_bot?start=join_AB12CD34" in card
    assert "Ссылка" not in handlers.format_band_card(band)
