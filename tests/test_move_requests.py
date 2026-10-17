"""Step 9: ask the owners of a booking to move it to another slot, and move it when they agree."""
from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import select

from app.db import models
from app.services import bookings, move_requests
from app.services.errors import ConflictError, ForbiddenError, NotFoundError

NOW = datetime(2026, 10, 14, 12, 0)  # Wednesday
DAY = date(2026, 10, 15)


@pytest.fixture
def now(freeze_now):
    freeze_now(NOW)


@pytest.fixture
async def world(session, make_user, make_band, now):
    """Band A holds Thursday 15:00-16:00; `asker` belongs to another band and wants that time."""
    leader = await make_user(last_name="Петров", first_name="Иван")
    band = await make_band(leader, "Rockers")
    booking = await bookings.create_booking(session, leader.telegram_id, band.id, DAY, time(15), time(16), "self", "Song")
    asker = await make_user(last_name="Сидоров", first_name="Пётр")
    await make_band(asker, "Dancers")
    return type("World", (), {"leader": leader, "band": band, "booking": booking, "asker": asker})


def _events(published, kind):
    return [event for event in published if event["type"] == kind]


async def _ask(session, world, start=time(17), end=time(18), message=None, booking=None, asker=None):
    return await move_requests.create_request(
        session,
        (asker or world.asker).telegram_id,
        (booking or world.booking).id,
        DAY,
        start,
        end,
        message,
    )


async def test_request_is_stored_and_the_responsible_person_is_asked(session, world, published):
    published.clear()
    request = await _ask(session, world, message="Нужен концертный прогон")

    assert request.status == "pending"
    assert request.expires_at == NOW + timedelta(hours=24)
    [event] = _events(published, "move_request")
    assert event["telegram_id"] == world.leader.telegram_id
    assert event["request_id"] == request.id
    assert "Сидоров" in event["text"] and "Rockers" in event["text"]
    assert "15:00" in event["text"] and "17:00" in event["text"] and "Нужен концертный прогон" in event["text"]
    assert event["accept"] and event["decline"]


async def test_request_expires_before_the_booking_starts(session, world, make_user, make_band, now):
    soon = await bookings.create_booking(
        session, world.leader.telegram_id, world.band.id, NOW.date(), time(14), time(15), "self", None
    )
    request = await move_requests.create_request(
        session, world.asker.telegram_id, soon.id, NOW.date(), time(18), time(19), None
    )
    assert request.expires_at == datetime.combine(NOW.date(), time(14))


async def test_notification_is_skipped_when_the_recipient_opted_out(session, world, published):
    world.leader.notifications_enabled = False
    await session.commit()
    published.clear()
    request = await _ask(session, world)
    assert request.status == "pending"
    assert _events(published, "move_request") == []


async def test_band_member_cannot_ask_to_move_their_own_booking(session, world):
    with pytest.raises(ForbiddenError):
        await _ask(session, world, asker=world.leader)


async def test_staff_booking_cannot_be_requested(session, world):
    world.booking.purpose = "staff"
    await session.commit()
    with pytest.raises(ConflictError):
        await _ask(session, world)


async def test_started_or_cancelled_booking_cannot_be_requested(session, world, freeze_now):
    world.booking.status = "cancelled"
    await session.commit()
    with pytest.raises(ConflictError):
        await _ask(session, world)
    world.booking.status = "confirmed"
    await session.commit()
    freeze_now(datetime(2026, 10, 15, 15, 30))
    with pytest.raises(ConflictError):
        await _ask(session, world, start=time(18), end=time(19))


async def test_proposed_slot_must_be_free_and_of_the_same_length(session, world):
    other = await bookings.create_booking(
        session, world.asker.telegram_id, (await _band_id(session, world.asker)), DAY, time(17), time(18), "self", None
    )
    with pytest.raises(ConflictError):
        await _ask(session, world, start=time(17), end=time(18))  # taken by `other`
    with pytest.raises(ConflictError):
        await _ask(session, world, start=time(19), end=time(20, 30))  # 1.5 h instead of 1 h
    assert other.id


async def _band_id(session, user):
    return (await session.execute(select(models.BandMember.band_id).where(models.BandMember.user_id == user.id))).scalar_one()


async def test_one_pending_request_per_booking_and_three_per_person(session, world, make_user, make_band):
    await _ask(session, world)
    with pytest.raises(ConflictError):
        await _ask(session, world, start=time(18), end=time(19))

    extra = []
    for hour in (16, 18, 19):
        owner = await make_user()
        band = await make_band(owner, f"B{hour}")
        extra.append(
            await bookings.create_booking(session, owner.telegram_id, band.id, DAY, time(hour), time(hour + 1), "self", None)
        )
    await _ask(session, world, start=time(10), end=time(11), booking=extra[0])
    await _ask(session, world, start=time(11), end=time(12), booking=extra[1])
    with pytest.raises(ConflictError):
        await _ask(session, world, start=time(12), end=time(13), booking=extra[2])


async def test_accepting_moves_the_booking_and_tells_the_asker(session, world, published):
    request = await _ask(session, world)
    published.clear()

    accepted = await move_requests.accept(session, world.leader.telegram_id, request.id)

    assert accepted.status == "accepted" and accepted.resolved_by_id == world.leader.id
    await session.refresh(world.booking)
    assert (world.booking.start_time, world.booking.end_time) == (time(17), time(18))
    [notice] = [event for event in _events(published, "notify") if event["telegram_id"] == world.asker.telegram_id]
    assert "15:00" in notice["text"] and "согласил" in notice["text"]
    change = (await session.execute(select(models.ScheduleChange).where(models.ScheduleChange.action == "updated"))).scalars().all()
    assert change and change[-1].actor_id == world.leader.id


async def test_responsible_person_who_is_not_an_owner_may_answer(session, world, make_user):
    helper = await make_user()
    session.add(models.BandMember(band_id=world.band.id, user_id=helper.id, instrument_role="other"))
    world.band.responsible_user_id = helper.id
    await session.commit()
    request = await _ask(session, world)

    accepted = await move_requests.accept(session, helper.telegram_id, request.id)
    assert accepted.status == "accepted"


async def test_stranger_cannot_answer(session, world, make_user):
    request = await _ask(session, world)
    stranger = await make_user()
    with pytest.raises(ForbiddenError):
        await move_requests.accept(session, stranger.telegram_id, request.id)
    with pytest.raises(ForbiddenError):
        await move_requests.decline(session, world.asker.telegram_id, request.id)


async def test_accept_fails_when_the_slot_was_taken_meanwhile(session, world, make_user, make_band):
    request = await _ask(session, world)
    taker = await make_user()
    band = await make_band(taker, "Late")
    await bookings.create_booking(session, taker.telegram_id, band.id, DAY, time(17), time(18), "self", None)

    with pytest.raises(ConflictError):
        await move_requests.accept(session, world.leader.telegram_id, request.id)
    await session.refresh(request)
    await session.refresh(world.booking)
    assert request.status == "pending"
    assert world.booking.start_time == time(15)


async def test_declining_tells_the_asker(session, world, published):
    request = await _ask(session, world)
    published.clear()

    declined = await move_requests.decline(session, world.leader.telegram_id, request.id)

    assert declined.status == "declined"
    [notice] = _events(published, "notify")
    assert notice["telegram_id"] == world.asker.telegram_id and "отказ" in notice["text"].lower()
    with pytest.raises(ConflictError):
        await move_requests.accept(session, world.leader.telegram_id, request.id)


async def test_asker_can_withdraw_silently(session, world, published):
    request = await _ask(session, world)
    published.clear()

    cancelled = await move_requests.cancel(session, world.asker.telegram_id, request.id)

    assert cancelled.status == "cancelled"
    assert _events(published, "notify") == []
    with pytest.raises(ForbiddenError):
        await move_requests.cancel(session, world.leader.telegram_id, request.id)


async def test_answering_after_the_deadline_expires_the_request(session, world, freeze_now):
    request = await _ask(session, world)
    freeze_now(NOW + timedelta(hours=25))

    with pytest.raises(ConflictError):
        await move_requests.accept(session, world.leader.telegram_id, request.id)
    await session.refresh(request)
    assert request.status == "expired"


async def test_expire_stale_handles_deadline_cancelled_and_started_bookings(session, world, published, freeze_now):
    request = await _ask(session, world)
    published.clear()
    assert await move_requests.expire_stale(session) == 0

    freeze_now(NOW + timedelta(hours=25))
    assert await move_requests.expire_stale(session) == 1
    await session.refresh(request)
    assert request.status == "expired"
    [notice] = _events(published, "notify")
    assert notice["telegram_id"] == world.asker.telegram_id

    freeze_now(NOW)
    second = await _ask(session, world, start=time(18), end=time(19))
    world.booking.status = "cancelled"
    await session.commit()
    assert await move_requests.expire_stale(session) == 1
    await session.refresh(second)
    assert second.status == "expired"


async def test_lists_show_incoming_to_managers_and_outgoing_to_the_asker(session, world, make_user):
    request = await _ask(session, world)

    mine = await move_requests.list_for_user(session, world.leader.telegram_id)
    assert [row["id"] for row in mine["incoming"]] == [request.id] and mine["outgoing"] == []
    assert mine["incoming"][0]["requester_name"].startswith("Сидоров")
    assert mine["incoming"][0]["proposed_start"] == time(17)

    theirs = await move_requests.list_for_user(session, world.asker.telegram_id)
    assert [row["id"] for row in theirs["outgoing"]] == [request.id] and theirs["incoming"] == []

    stranger = await make_user()
    nothing = await move_requests.list_for_user(session, stranger.telegram_id)
    assert nothing == {"incoming": [], "outgoing": []}


async def test_unknown_request(session, world):
    with pytest.raises(NotFoundError):
        await move_requests.accept(session, world.leader.telegram_id, 999)


async def test_accepting_expires_other_open_requests_for_the_same_booking(session, world, make_user, make_band, published):
    first = await _ask(session, world)
    second_asker = await make_user()
    await make_band(second_asker, "Third")
    second = await _ask(session, world, start=time(18), end=time(19), asker=second_asker)
    published.clear()

    await move_requests.accept(session, world.leader.telegram_id, first.id)

    await session.refresh(second)
    assert second.status == "expired"
    assert second_asker.telegram_id in {event["telegram_id"] for event in _events(published, "notify")}


# ---------- HTTP ----------


import httpx  # noqa: E402
import pytest_asyncio  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.main import app  # noqa: E402
from tests.test_webapp_auth import BOT_TOKEN, init_data  # noqa: E402

AUTH = {"X-Internal-Token": settings.internal_api_token}


@pytest_asyncio.fixture
async def client(session, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", BOT_TOKEN)

    async def _session_override():
        yield session

    app.dependency_overrides[get_session] = _session_override
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


def signed(user) -> dict[str, str]:
    return {"X-Telegram-Init-Data": init_data(user.telegram_id)}


def body(world, **extra):
    return {"booking_id": world.booking.id, "proposed_date": "2026-10-15", "start_time": "17:00", "end_time": "18:00", **extra}


async def test_webapp_flow_ask_list_accept(client, session, world):
    created = await client.post("/webapp/api/move-requests", json=body(world, message="Пожалуйста"), headers=signed(world.asker))
    assert created.status_code == 200 and created.json()["status"] == "pending"
    request_id = created.json()["id"]

    outgoing = (await client.get("/webapp/api/move-requests", headers=signed(world.asker))).json()
    assert [row["id"] for row in outgoing["outgoing"]] == [request_id]
    assert outgoing["outgoing"][0]["proposed_start"] == "17:00:00" and outgoing["outgoing"][0]["booking_date"] == "2026-10-15"

    incoming = (await client.get("/webapp/api/move-requests", headers=signed(world.leader))).json()
    assert [row["id"] for row in incoming["incoming"]] == [request_id]
    assert incoming["incoming"][0]["message"] == "Пожалуйста"

    answered = await client.post(f"/webapp/api/move-requests/{request_id}/accept", headers=signed(world.leader))
    assert answered.status_code == 200 and answered.json()["status"] == "accepted"
    await session.refresh(world.booking)
    assert world.booking.start_time == time(17)


async def test_webapp_errors_map_to_http_codes(client, world, make_user):
    assert (await client.post("/webapp/api/move-requests", json=body(world), headers=signed(world.leader))).status_code == 403
    assert (await client.post("/webapp/api/move-requests", json=body(world, end_time="19:00"), headers=signed(world.asker))).status_code == 409
    request_id = (await client.post("/webapp/api/move-requests", json=body(world), headers=signed(world.asker))).json()["id"]
    stranger = await make_user()
    assert (await client.post(f"/webapp/api/move-requests/{request_id}/decline", headers=signed(stranger))).status_code == 403
    assert (await client.post(f"/webapp/api/move-requests/{request_id}/cancel", headers=signed(world.asker))).json()["status"] == "cancelled"
    assert (await client.post("/webapp/api/move-requests", json=body(world))).status_code in {401, 403}


async def test_schedule_marks_the_viewers_own_bookings(client, world):
    mine = (await client.get("/webapp/api/schedule", headers=signed(world.leader))).json()["bookings"][0]
    theirs = (await client.get("/webapp/api/schedule", headers=signed(world.asker))).json()["bookings"][0]
    anonymous = (await client.get("/webapp/api/schedule")).json()["bookings"][0]
    assert mine["is_mine"] is True and theirs["is_mine"] is False
    assert "is_mine" not in anonymous


async def test_internal_answer_routes_for_the_bot(client, session, world):
    request = await _ask(session, world)
    assert (await client.post(f"/move-requests/{request.id}/accept", json={"telegram_id": world.leader.telegram_id})).status_code == 401
    answered = await client.post(f"/move-requests/{request.id}/accept", json={"telegram_id": world.leader.telegram_id}, headers=AUTH)
    assert answered.status_code == 200 and answered.json()["status"] == "accepted"

    second = await _ask(session, world, start=time(18), end=time(19))
    declined = await client.post(f"/move-requests/{second.id}/decline", json={"telegram_id": world.leader.telegram_id}, headers=AUTH)
    assert declined.json()["status"] == "declined"
