"""HTTP-level smoke tests: routing, auth header, validation and the ngrok exposure guard."""
from datetime import datetime

import httpx
import pytest
import pytest_asyncio

from app.core.config import settings
from app.db.session import get_session
from app.main import app

FRIDAY = datetime(2026, 10, 16, 12, 0)
AUTH = {"X-Internal-Token": settings.internal_api_token}


@pytest_asyncio.fixture
async def client(session):
    async def _session_override():
        yield session

    app.dependency_overrides[get_session] = _session_override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def test_internal_token_is_required(client):
    assert (await client.get("/planning/week")).status_code == 401
    assert (await client.get("/planning/week", headers=AUTH)).status_code == 200


async def test_submit_list_and_delete_request_over_http(client, session, make_user, make_band, planning_on, freeze_now):
    freeze_now(FRIDAY)
    leader = await make_user()
    band = await make_band(leader, "Rockers")
    body = {
        "telegram_id": leader.telegram_id,
        "band_id": band.id,
        "duration_minutes": 60,
        "purpose": "self",
        "song_title": "Song",
        "windows": [{"day_of_week": 2, "start_time": "14:00:00", "end_time": "16:00:00"}],
    }
    created = await client.post("/planning/requests", json=body, headers=AUTH)
    assert created.status_code == 200, created.text
    listed = await client.get("/planning/requests", params={"telegram_id": leader.telegram_id}, headers=AUTH)
    assert [r["band_name"] for r in listed.json()] == ["Rockers"]
    deleted = await client.delete(f"/planning/requests/{created.json()['id']}", params={"telegram_id": leader.telegram_id}, headers=AUTH)
    assert deleted.status_code == 200


async def test_business_errors_map_to_http_status_codes(client, make_user, make_band, planning_on, freeze_now):
    freeze_now(FRIDAY)
    leader = await make_user()
    band = await make_band(leader, "Rockers")
    stranger = await make_user()
    too_short = {
        "telegram_id": leader.telegram_id,
        "band_id": band.id,
        "duration_minutes": 120,
        "windows": [{"day_of_week": 2, "start_time": "14:00:00", "end_time": "15:00:00"}],
    }
    assert (await client.post("/planning/requests", json=too_short, headers=AUTH)).status_code == 409
    forbidden = {**too_short, "telegram_id": stranger.telegram_id, "duration_minutes": 60}
    assert (await client.post("/planning/requests", json=forbidden, headers=AUTH)).status_code == 403
    invalid = {**too_short, "windows": []}
    assert (await client.post("/planning/requests", json=invalid, headers=AUTH)).status_code == 422


async def test_allocate_endpoint_requires_admin(client, make_user, planning_on, freeze_now):
    freeze_now(FRIDAY)
    plain = await make_user()
    admin = await make_user(role="admin")
    denied = await client.post("/planning/allocate", json={"telegram_id": plain.telegram_id}, headers=AUTH)
    assert denied.status_code == 403
    dry = await client.post("/planning/allocate", json={"telegram_id": admin.telegram_id, "dry_run": True}, headers=AUTH)
    assert dry.status_code == 200 and dry.json()["published"] is False


async def test_tunnel_traffic_only_reaches_the_mini_app(client):
    tunnel = {"X-Forwarded-Host": "example.ngrok-free.app"}
    assert (await client.get("/planning/week", headers={**AUTH, **tunnel})).status_code == 404
    assert (await client.get("/health", headers=tunnel)).status_code == 200
