import pytest
from fastapi import HTTPException

from app.core import config, security
from app.core.config import Settings, validate_internal_token

STRONG = "x" * 32


async def test_internal_token_accepts_exact_match(monkeypatch):
    monkeypatch.setattr(security.settings, "internal_api_token", STRONG)
    assert await security.verify_internal_token(STRONG) is None


@pytest.mark.parametrize("header", [None, "", "wrong", STRONG + "x", "ключ"])
async def test_internal_token_rejects_bad_header(monkeypatch, header):
    monkeypatch.setattr(security.settings, "internal_api_token", STRONG)
    with pytest.raises(HTTPException) as exc:
        await security.verify_internal_token(header)
    assert exc.value.status_code == 401


async def test_internal_token_rejects_empty_header_when_token_is_empty(monkeypatch):
    monkeypatch.setattr(security.settings, "internal_api_token", "")
    for header in (None, ""):
        with pytest.raises(HTTPException):
            await security.verify_internal_token(header)


def _settings(environment: str, token: str) -> Settings:
    return Settings(_env_file=None, environment=environment, internal_api_token=token)


def test_local_environment_allows_weak_token():
    validate_internal_token(_settings("local", "change-me"))


@pytest.mark.parametrize("environment", ["local", "production"])
def test_empty_token_is_never_allowed(environment):
    with pytest.raises(RuntimeError):
        validate_internal_token(_settings(environment, "  "))


@pytest.mark.parametrize("token", ["change-me", "short-secret", "y" * (config.MIN_INTERNAL_TOKEN_LENGTH - 1)])
def test_non_local_environment_rejects_default_or_short_token(token):
    with pytest.raises(RuntimeError):
        validate_internal_token(_settings("production", token))


def test_non_local_environment_accepts_strong_token():
    validate_internal_token(_settings("production", STRONG))
