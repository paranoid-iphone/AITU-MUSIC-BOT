"""Step 10: waiting list for occupied time, offers when it frees up, taking it."""
from datetime import date, datetime, time

import pytest
from sqlalchemy import select

from app.db import models
from app.services import bands, bookings, waitlist
from app.services.errors import ConflictError, ForbiddenError, NotFoundError

NOW = datetime(2026, 10, 14, 12, 0)  # Wednesday
DAY = date(2026, 10, 15)


@pytest.fixture
def now(freeze_now):
    freeze_now(NOW)


@pytest.fixture
async def world(session, make_user, make_band, now):
    """Rockers hold Thursday 15:00-16:00; `waiter` belongs to Dancers and wants that time."""
    leader = await make_user(last_name="Петров", first_name="Иван")
    band = await make_band(leader, "Rockers")
    booking = await bookings.create_booking(session, leader.telegram_id, band.id, DAY, time(15), time(16), "self", None)
    waiter = await make_user(last_name="Сидоров", first_name="Пётр")
    waiter_band = await make_band(waiter, "Dancers")
    return type("World", (), {"leader": leader, "band": band, "booking": booking, "waiter": waiter, "waiter_band": waiter_band})


def _offers(published):
    return [event for event in published if event["type"] == "waitlist_offer"]


async def _join(session, world, start=time(15), end=time(16), user=None, band=None):
    return await waitlist.join(
        session, (user or world.waiter).telegram_id, (band or world.waiter_band).id, DAY, start, end
    )


async def _entries(session):
    return (await session.execute(select(models.WaitlistEntry))).scalars().all()


async def test_join_stores_the_entry(session, world):
    entry = await _join(session, world)
    assert (entry.user_id, entry.band_id, entry.booking_date) == (world.waiter.id, world.waiter_band.id, DAY)
    assert (entry.start_time, entry.end_time) == (time(15), time(16))


async def test_cannot_wait_for_free_time(session, world):
    with pytest.raises(ConflictError):
        await _join(session, world, time(17), time(18))


async def test_a_partly_occupied_range_can_be_waited_for(session, world):
    entry = await _join(session, world, time(14, 30), time(15, 30))
    assert entry.id


async def test_range_must_be_valid(session, world):
    with pytest.raises(ConflictError):
        await _join(session, world, time(15), time(15, 45))  # not a 30-minute step
    with pytest.raises(ConflictError):
        await waitlist.join(session, world.waiter.telegram_id, world.waiter_band.id, date(2026, 10, 13), time(15), time(16))


async def test_cannot_wait_for_a_staff_booking(session, world):
    world.booking.purpose = "staff"
    await session.commit()
    with pytest.raises(ConflictError):
        await _join(session, world)


async def test_only_members_of_the_band_and_approved_users_may_wait(session, world, make_user):
    outsider = await make_user()
    with pytest.raises(ForbiddenError):
        await _join(session, world, user=outsider)
    pending = await make_user(status="pending")
    with pytest.raises(ForbiddenError):
        await waitlist.join(session, pending.telegram_id, None, DAY, time(15), time(16))


async def test_personal_waiting_follows_the_personal_setting(session, world):
    entry = await waitlist.join(session, world.waiter.telegram_id, None, DAY, time(15), time(16))
    assert entry.band_id is None
    session.add(models.ClubSetting(key="personal_bookings_enabled", value="0"))
    await session.commit()
    with pytest.raises(ForbiddenError):
        await waitlist.join(session, world.leader.telegram_id, None, DAY, time(15), time(16))


async def test_duplicates_and_the_per_person_limit(session, world):
    await _join(session, world)
    with pytest.raises(ConflictError):
        await _join(session, world)
    # fill the list with distinct ranges overlapping the existing booking
    for start, end in [(time(14, 30), time(15, 30)), (time(15, 30), time(16, 30)), (time(14), time(15, 30)), (time(15), time(17))]:
        await _join(session, world, start, end)
    with pytest.raises(ConflictError):
        await _join(session, world, time(14, 30), time(16, 30))


async def test_cancelling_the_booking_offers_the_time_to_the_waiter(session, world, published):
    entry = await _join(session, world)
    published.clear()
    await bookings.delete_booking(session, world.leader.telegram_id, world.booking.id)
    [offer] = _offers(published)
    assert offer["telegram_id"] == world.waiter.telegram_id
    assert offer["entry_id"] == entry.id
    assert "15:00" in offer["text"] and "Dancers" in offer["text"]
    assert "15:00" in offer["button"]


async def test_moving_the_booking_away_offers_the_old_time(session, world, published):
    entry = await _join(session, world)
    published.clear()
    await bookings.update_booking(session, world.leader.telegram_id, world.booking.id, DAY, time(17), time(18))
    [offer] = _offers(published)
    assert offer["entry_id"] == entry.id


async def test_editing_the_song_frees_nothing(session, world, published):
    await _join(session, world)
    published.clear()
    await bookings.update_booking(session, world.leader.telegram_id, world.booking.id, song_title="New song")
    assert _offers(published) == []


async def test_a_partly_free_range_is_not_offered(session, world, published, make_user, make_band):
    other = await make_user()
    other_band = await make_band(other, "Others")
    second = await bookings.create_booking(session, other.telegram_id, other_band.id, DAY, time(16), time(17), "self", None)
    await _join(session, world, time(15), time(17))
    published.clear()
    await bookings.delete_booking(session, world.leader.telegram_id, world.booking.id)
    assert _offers(published) == []  # 16:00-17:00 is still taken
    await bookings.delete_booking(session, other.telegram_id, second.id)
    assert len(_offers(published)) == 1


async def test_offers_follow_queue_order_and_skip_unrelated_entries(session, world, published, make_user, make_band):
    second = await make_user(last_name="Иванов")
    second_band = await make_band(second, "Singers")
    first_entry = await _join(session, world)
    second_entry = await waitlist.join(session, second.telegram_id, second_band.id, DAY, time(15), time(16))
    unrelated = await _join(session, world, time(14, 30), time(15, 30))
    await bookings.create_booking(
        session, world.leader.telegram_id, world.band.id, date(2026, 10, 16), time(15), time(16), "self", None
    )
    other_day = await waitlist.join(session, second.telegram_id, second_band.id, date(2026, 10, 16), time(15), time(16))
    published.clear()
    await bookings.delete_booking(session, world.leader.telegram_id, world.booking.id)
    # queue order is creation order; `unrelated` overlaps the freed hour so it is offered too, the other day is not
    assert [offer["entry_id"] for offer in _offers(published)] == [first_entry.id, second_entry.id, unrelated.id]
    assert other_day.id not in [offer["entry_id"] for offer in _offers(published)]


async def test_no_offer_when_notifications_are_off(session, world, published):
    world.waiter.notifications_enabled = False
    await session.commit()
    entry = await _join(session, world)
    published.clear()
    await bookings.delete_booking(session, world.leader.telegram_id, world.booking.id)
    assert _offers(published) == []
    [row] = await waitlist.list_for_user(session, world.waiter.telegram_id)
    assert row["id"] == entry.id and row["is_free"] is True


async def test_taking_books_the_range_and_removes_the_entry(session, world):
    entry = await _join(session, world)
    await bookings.delete_booking(session, world.leader.telegram_id, world.booking.id)
    booking = await waitlist.take(session, world.waiter.telegram_id, entry.id)
    assert (booking.band_id, booking.booking_date, booking.start_time, booking.end_time) == (world.waiter_band.id, DAY, time(15), time(16))
    assert booking.purpose == "self"
    assert await _entries(session) == []


async def test_taking_a_personal_entry_books_a_personal_rehearsal(session, world):
    entry = await waitlist.join(session, world.waiter.telegram_id, None, DAY, time(15), time(16))
    await bookings.delete_booking(session, world.leader.telegram_id, world.booking.id)
    booking = await waitlist.take(session, world.waiter.telegram_id, entry.id)
    assert booking.band_id is None and booking.purpose == "personal"


async def test_taking_taken_time_fails_and_keeps_the_entry(session, world):
    entry = await _join(session, world)
    with pytest.raises(ConflictError):
        await waitlist.take(session, world.waiter.telegram_id, entry.id)  # still booked by Rockers
    assert [item.id for item in await _entries(session)] == [entry.id]


async def test_first_to_take_wins(session, world, make_user, make_band):
    second = await make_user(last_name="Иванов")
    second_band = await make_band(second, "Singers")
    first_entry = await _join(session, world)
    second_entry = await waitlist.join(session, second.telegram_id, second_band.id, DAY, time(15), time(16))
    await bookings.delete_booking(session, world.leader.telegram_id, world.booking.id)
    await waitlist.take(session, second.telegram_id, second_entry.id)
    with pytest.raises(ConflictError):
        await waitlist.take(session, world.waiter.telegram_id, first_entry.id)
    assert [item.id for item in await _entries(session)] == [first_entry.id]


async def test_only_the_owner_can_take_or_leave(session, world):
    entry = await _join(session, world)
    with pytest.raises(NotFoundError):
        await waitlist.take(session, world.leader.telegram_id, entry.id)
    with pytest.raises(NotFoundError):
        await waitlist.leave(session, world.leader.telegram_id, entry.id)
    await waitlist.leave(session, world.waiter.telegram_id, entry.id)
    assert await _entries(session) == []


async def test_list_marks_free_entries_and_drops_past_ones(session, world, freeze_now):
    await _join(session, world)
    [row] = await waitlist.list_for_user(session, world.waiter.telegram_id)
    assert row["is_free"] is False and row["band_name"] == "Dancers"
    await bookings.delete_booking(session, world.leader.telegram_id, world.booking.id)
    [row] = await waitlist.list_for_user(session, world.waiter.telegram_id)
    assert row["is_free"] is True
    freeze_now(datetime(2026, 10, 15, 15, 1))
    assert await waitlist.list_for_user(session, world.waiter.telegram_id) == []
    assert await _entries(session) == []


async def test_cleanup_removes_only_started_entries(session, world, freeze_now):
    await _join(session, world)
    assert await waitlist.cleanup_past(session) == 0
    freeze_now(datetime(2026, 10, 15, 15, 0))
    assert await waitlist.cleanup_past(session) == 1


async def test_removing_a_member_drops_their_entries(session, world, make_user):
    teammate = await make_user()
    await bands.join_band(session, teammate.telegram_id, world.waiter_band.invite_code, "drums")
    await waitlist.join(session, teammate.telegram_id, world.waiter_band.id, DAY, time(15), time(16))
    await _join(session, world)
    member_id = (
        await session.execute(
            select(models.BandMember.id).where(models.BandMember.band_id == world.waiter_band.id, models.BandMember.user_id == teammate.id)
        )
    ).scalar_one()
    await bands.remove_band_member(session, world.waiter_band.id, member_id, world.waiter.telegram_id)
    assert [item.user_id for item in await _entries(session)] == [world.waiter.id]


async def test_deleting_the_band_drops_its_entries(session, world):
    await _join(session, world)
    await bands.delete_band(session, world.waiter_band.id, world.waiter.telegram_id)
    assert await _entries(session) == []


# ---- HTTP: Mini App routes and the bot's internal route ----

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


def payload(world, **extra):
    return {"band_id": world.waiter_band.id, "booking_date": "2026-10-15", "start_time": "15:00", "end_time": "16:00", **extra}


async def test_webapp_join_list_take_and_leave(client, session, world):
    joined = await client.post("/webapp/api/waitlist", json=payload(world), headers=signed(world.waiter))
    assert joined.status_code == 200
    entry_id = joined.json()["id"]

    listed = (await client.get("/webapp/api/waitlist", headers=signed(world.waiter))).json()["entries"]
    assert [(row["id"], row["is_free"], row["band_name"]) for row in listed] == [(entry_id, False, "Dancers")]
    assert listed[0]["booking_date"] == "2026-10-15" and listed[0]["start_time"] == "15:00:00"
    assert (await client.get("/webapp/api/waitlist", headers=signed(world.leader))).json()["entries"] == []

    await bookings.delete_booking(session, world.leader.telegram_id, world.booking.id)
    assert (await client.get("/webapp/api/waitlist", headers=signed(world.waiter))).json()["entries"][0]["is_free"] is True
    taken = await client.post(f"/webapp/api/waitlist/{entry_id}/take", headers=signed(world.waiter))
    assert taken.status_code == 200 and taken.json()["band_id"] == world.waiter_band.id

    assert (await client.post(f"/webapp/api/waitlist/{entry_id}/leave", headers=signed(world.waiter))).status_code == 404  # gone with the booking
    outsider = await client.post("/webapp/api/waitlist", json=payload(world), headers=signed(world.leader))
    assert outsider.status_code == 403  # the leader is not in Dancers


async def test_webapp_errors_map_to_http_codes(client, world):
    assert (await client.post("/webapp/api/waitlist", json=payload(world, start_time="17:00", end_time="18:00"), headers=signed(world.waiter))).status_code == 409
    assert (await client.post("/webapp/api/waitlist", json=payload(world))).status_code in {401, 403}
    entry_id = (await client.post("/webapp/api/waitlist", json=payload(world), headers=signed(world.waiter))).json()["id"]
    assert (await client.post(f"/webapp/api/waitlist/{entry_id}/leave", headers=signed(world.leader))).status_code == 404
    assert (await client.post(f"/webapp/api/waitlist/{entry_id}/take", headers=signed(world.waiter))).status_code == 409


async def test_internal_take_route_for_the_bot(client, session, world):
    entry = await _join(session, world)
    body = {"telegram_id": world.waiter.telegram_id}
    leader_id, booking_id = world.leader.telegram_id, world.booking.id  # a failed take rolls back and expires the objects
    assert (await client.post(f"/waitlist/{entry.id}/take", json=body)).status_code == 401
    assert (await client.post(f"/waitlist/{entry.id}/take", json=body, headers=AUTH)).status_code == 409  # still booked
    await bookings.delete_booking(session, leader_id, booking_id)
    taken = await client.post(f"/waitlist/{entry.id}/take", json=body, headers=AUTH)
    assert taken.status_code == 200 and taken.json()["start_time"] == "15:00:00"
