import logging

import pytest

from app.bot import webapp_url
from app.core import logging_setup
from app.core.config import Settings, settings


@pytest.fixture(autouse=True)
def _reset_url_cache(monkeypatch):
    monkeypatch.setattr(webapp_url, "_cached_url", None)
    monkeypatch.setattr(webapp_url, "_cached_until", None)


async def test_explicit_base_url_wins_over_domain(monkeypatch):
    monkeypatch.setattr(settings, "webapp_base_url", "https://example.org/")
    monkeypatch.setattr(settings, "ngrok_domain", "club.ngrok-free.app")
    assert await webapp_url.get_webapp_base_url() == "https://example.org"


@pytest.mark.parametrize("raw", ["club.ngrok-free.app", "https://club.ngrok-free.app/", " HTTP://club.ngrok-free.app "])
async def test_ngrok_domain_gives_stable_url_without_asking_ngrok(monkeypatch, raw):
    monkeypatch.setattr(settings, "webapp_base_url", "")
    monkeypatch.setattr(settings, "ngrok_domain", raw)

    async def _boom():
        raise AssertionError("ngrok API must not be queried")

    monkeypatch.setattr(webapp_url, "_fetch_ngrok_url", _boom)
    assert await webapp_url.get_webapp_base_url() == "https://club.ngrok-free.app"


async def test_falls_back_to_ngrok_api_without_domain(monkeypatch):
    monkeypatch.setattr(settings, "webapp_base_url", "")
    monkeypatch.setattr(settings, "ngrok_domain", "")

    async def _fetch():
        return "https://random.ngrok.app"

    monkeypatch.setattr(webapp_url, "_fetch_ngrok_url", _fetch)
    assert await webapp_url.get_webapp_base_url() == "https://random.ngrok.app"


def test_setup_logging_applies_level_and_quiets_noisy_loggers(monkeypatch):
    calls = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: calls.update(kwargs))
    monkeypatch.setattr(settings, "log_level", "debug")
    logging_setup.setup_logging()
    assert calls["level"] == logging.DEBUG
    assert "%(name)s" in calls["format"]
    for name in logging_setup.NOISY_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING


def test_setup_logging_falls_back_to_info_on_garbage(monkeypatch):
    calls = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: calls.update(kwargs))
    monkeypatch.setattr(settings, "log_level", "loud")
    logging_setup.setup_logging()
    assert calls["level"] == logging.INFO


def test_sql_echo_defaults_off():
    assert settings.sql_echo is False


def test_env_file_may_hold_compose_only_variables(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("BACKUP_INTERVAL_HOURS=24\nBACKUP_KEEP_DAYS=14\nLOG_LEVEL=WARNING\n", encoding="utf-8")
    loaded = Settings(_env_file=env_file)
    assert loaded.log_level == "WARNING"
