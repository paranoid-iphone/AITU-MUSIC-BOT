"""Telegram Mini App initData validation and the webapp auth dependencies."""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI

from app.api.webapp_auth import current_webapp_user, fresh_webapp_user
from app.core.config import settings
from app.core.telegram_auth import validate_init_data
from app.db.session import get_session
from app.services.errors import AppError

BOT_TOKEN = "123456:TEST-TOKEN"


def sign(fields: dict[str, str], bot_token: str = BOT_TOKEN) -> str:
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    return urlencode({**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})


def init_data(telegram_id: int = 1, age: int = 0, **extra: str) -> str:
    fields = {
        "auth_date": str(int(time.time()) - age),
        "user": json.dumps({"id": telegram_id, "first_name": "Test"}),
        **extra,
    }
    return sign(fields)


def test_valid_signature_returns_telegram_user():
    user = validate_init_data(init_data(telegram_id=42), BOT_TOKEN)
    assert user is not None and user["id"] == 42


def test_tampered_hash_is_rejected():
    data = init_data(telegram_id=42)
    assert validate_init_data(data.replace("42", "43"), BOT_TOKEN) is None
    assert validate_init_data(data[:-1] + ("0" if data[-1] != "0" else "1"), BOT_TOKEN) is None


def test_signature_made_with_another_token_is_rejected():
    assert validate_init_data(sign({"auth_date": str(int(time.time())), "user": '{"id": 1}'}, "999:OTHER"), BOT_TOKEN) is None


def test_expired_auth_date_is_rejected():
    assert validate_init_data(init_data(age=2 * 24 * 3600), BOT_TOKEN) is None
    assert validate_init_data(init_data(age=7200), BOT_TOKEN, max_age_seconds=3600) is None
    assert validate_init_data(init_data(age=1800), BOT_TOKEN, max_age_seconds=3600) is not None


def test_future_auth_date_is_rejected():
    assert validate_init_data(init_data(age=-3600), BOT_TOKEN) is None
    # A few seconds of clock skew between Telegram and the server is tolerated.
    assert validate_init_data(init_data(age=-10), BOT_TOKEN) is not None


@pytest.mark.parametrize("value", [None, "", "garbage", "auth_date=1&user=%7B%7D"])
def test_missing_or_malformed_init_data_is_rejected(value):
    assert validate_init_data(value, BOT_TOKEN) is None


def test_empty_bot_token_never_validates():
    assert validate_init_data(init_data(), "") is None


@pytest_asyncio.fixture
async def client(session, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", BOT_TOKEN)
    probe = FastAPI()

    @probe.exception_handler(AppError)
    async def _handler(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @probe.get("/read")
    async def read(user=Depends(current_webapp_user)):
        return {"telegram_id": user.telegram_id}

    @probe.get("/write")
    async def write(user=Depends(fresh_webapp_user)):
        return {"telegram_id": user.telegram_id}

    async def _session_override():
        yield session

    probe.dependency_overrides[get_session] = _session_override
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=probe), base_url="http://test") as http:
        yield http


def headers(telegram_id: int, age: int = 0) -> dict[str, str]:
    return {"X-Telegram-Init-Data": init_data(telegram_id, age)}


async def test_missing_or_bad_signature_is_401(client):
    assert (await client.get("/read")).status_code == 401
    assert (await client.get("/read", headers={"X-Telegram-Init-Data": "hash=bad"})).status_code == 401


async def test_unregistered_telegram_user_is_403(client):
    assert (await client.get("/read", headers=headers(777_000))).status_code == 403


@pytest.mark.parametrize("status", ["pending", "rejected", "banned", "deleted"])
async def test_not_approved_user_is_403(client, make_user, status):
    user = await make_user(status=status)
    assert (await client.get("/read", headers=headers(user.telegram_id))).status_code == 403


async def test_approved_user_passes(client, make_user):
    user = await make_user()
    response = await client.get("/read", headers=headers(user.telegram_id))
    assert response.status_code == 200
    assert response.json() == {"telegram_id": user.telegram_id}


async def test_mutating_dependency_requires_fresh_init_data(client, make_user):
    user = await make_user()
    stale = headers(user.telegram_id, age=2 * 3600)
    assert (await client.get("/read", headers=stale)).status_code == 200
    assert (await client.get("/write", headers=stale)).status_code == 401
    assert (await client.get("/write", headers=headers(user.telegram_id, age=600))).status_code == 200
