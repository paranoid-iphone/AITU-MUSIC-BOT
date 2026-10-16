from datetime import date, datetime, time

import pytest
from sqlalchemy import select

from app.db import models
from app.services import bands, bookings, planning
from app.services.errors import ConflictError, ForbiddenError

FRIDAY = datetime(2026, 10, 16, 12, 0)  # next planning week: Mon 2026-10-19 .. Sun 2026-10-25
NEXT_MONDAY = date(2026, 10, 19)
NEXT_WED = date(2026, 10, 21)
WED = 2


def w(day, start_h, end_h):
    return {"day_of_week": day, "start_time": time(start_h), "end_time": time(end_h)}


@pytest.fixture(autouse=True)
def _planning(planning_on, freeze_now):
    freeze_now(FRIDAY)


async def _band(make_user, make_band, group, name):
    leader = await make_user(group_slug=group, last_name=name, first_name="Лидер")
    return leader, await make_band(leader, name)


async def _submit(session, leader, band, hours, *windows, purpose="self", song="Song"):
    return await planning.submit_request(session, leader.telegram_id, band.id, int(hours * 60), purpose, song, list(windows))


async def test_example_dance_and_music_share_wednesday(session, make_user, make_band, published):
    admin = await make_user(role="admin")
    dancer, dance = await _band(make_user, make_band, "dance", "Dancers")
    musician, music = await _band(make_user, make_band, "music", "Musicians")
    await _submit(session, dancer, dance, 2, w(WED, 13, 15))
    await _submit(session, musician, music, 3, w(WED, 14, 18))
    published.clear()

    summary = await planning.run_weekly_allocation(session, admin.telegram_id)

    assert [(r["band_name"], r["start_time"], r["end_time"]) for r in summary["assigned"]] == [
        ("Dancers", "13:00", "15:00"),
        ("Musicians", "15:00", "18:00"),
    ]
    assert summary["unassigned"] == []
    created = (await session.execute(select(models.Booking).order_by(models.Booking.start_time))).scalars().all()
    assert [(b.booking_date, b.source, b.status) for b in created] == [(NEXT_WED, "auto", "confirmed")] * 2
    week = await session.get(models.SchedulingWeek, NEXT_MONDAY)
    assert week.status == "published"
    statuses = {r.status for r in (await session.execute(select(models.AvailabilityRequest))).scalars()}
    assert statuses == {"assigned"}
    actions = {c.action for c in (await session.execute(select(models.ScheduleChange))).scalars()}
    assert actions == {"auto_assigned"}
    notified = {e["telegram_id"] for e in published if e["type"] == "notify"}
    assert notified == {dancer.telegram_id, musician.telegram_id}
    assert any(e["type"] == "schedule_changed" for e in published)


async def test_dry_run_changes_nothing(session, make_user, make_band):
    admin = await make_user(role="admin")
    leader, band = await _band(make_user, make_band, "music", "Rockers")
    await _submit(session, leader, band, 1, w(WED, 14, 16))

    summary = await planning.run_weekly_allocation(session, admin.telegram_id, dry_run=True)

    assert len(summary["assigned"]) == 1 and summary["published"] is False
    assert (await session.execute(select(models.Booking))).scalars().all() == []
    assert await session.get(models.SchedulingWeek, NEXT_MONDAY) is None
    request = (await session.execute(select(models.AvailabilityRequest))).scalar_one()
    assert request.status == "pending"


async def test_week_can_be_allocated_only_once_and_only_by_admin(session, make_user, make_band):
    admin = await make_user(role="admin")
    leader, band = await _band(make_user, make_band, "music", "Rockers")
    await _submit(session, leader, band, 1, w(WED, 14, 16))
    with pytest.raises(ForbiddenError):
        await planning.run_weekly_allocation(session, leader.telegram_id)
    await planning.run_weekly_allocation(session, admin.telegram_id)
    with pytest.raises(ConflictError):
        await planning.run_weekly_allocation(session, admin.telegram_id)


async def test_existing_bookings_are_respected_and_losers_are_notified(session, make_user, make_band, published):
    admin = await make_user(role="admin")
    dsvr = await make_user(role="dsvr", group_slug=None)
    leader, band = await _band(make_user, make_band, "dance", "Dancers")
    # DSVR already holds Wed 13-14 -> a 2h request inside 13-17 gets 14-16; a 1h request inside 13-14 gets nothing.
    await bookings.create_priority_booking(session, dsvr.telegram_id, NEXT_WED, time(13), time(14), "Мероприятие")
    other_leader, other = await _band(make_user, make_band, "music", "Musicians")
    await _submit(session, leader, band, 2, w(WED, 13, 17))
    await _submit(session, other_leader, other, 1, w(WED, 13, 14))
    published.clear()

    summary = await planning.run_weekly_allocation(session, admin.telegram_id)

    assert [(r["band_name"], r["start_time"]) for r in summary["assigned"]] == [("Dancers", "14:00")]
    assert [r["band_name"] for r in summary["unassigned"]] == ["Musicians"]
    texts = {e["telegram_id"]: e["text"] for e in published if e["type"] == "notify"}
    assert "Не удалось подобрать" in texts[other_leader.telegram_id]
    assert "Расписание на неделю составлено" in texts[leader.telegram_id]
    request = (
        await session.execute(select(models.AvailabilityRequest).where(models.AvailabilityRequest.band_id == other.id))
    ).scalar_one()
    assert request.status == "unassigned"


async def test_second_priority_is_used_when_first_is_taken(session, make_user, make_band):
    admin = await make_user(role="admin")
    a_leader, a = await _band(make_user, make_band, "music", "A")
    b_leader, b = await _band(make_user, make_band, "music", "B")
    await _submit(session, a_leader, a, 2, w(1, 18, 20), w(WED, 14, 18))
    await _submit(session, b_leader, b, 2, w(1, 18, 20), w(5, 12, 16))
    summary = await planning.run_weekly_allocation(session, admin.telegram_id)
    by_band = {r["band_name"]: (r["date"], r["start_time"], r["priority"]) for r in summary["assigned"]}
    assert by_band == {"A": ("2026-10-20", "18:00", 1), "B": ("2026-10-24", "12:00", 2)}


async def test_submit_validation(session, make_user, make_band):
    leader, band = await _band(make_user, make_band, "music", "Rockers")
    outsider = await make_user()
    with pytest.raises(ConflictError):
        await _submit(session, leader, band, 2, w(WED, 14, 15))  # window shorter than the rehearsal
    with pytest.raises(ConflictError):
        await _submit(session, leader, band, 1, w(WED, 6, 9))  # before the room opens
    with pytest.raises(ConflictError):
        await _submit(session, leader, band, 1, *[w(d, 10, 12) for d in range(6)])  # too many priorities
    with pytest.raises(ConflictError):
        await _submit(session, leader, band, 1, {"day_of_week": WED, "start_time": time(10, 15), "end_time": time(12)})
    with pytest.raises(ForbiddenError):
        await _submit(session, outsider, band, 1, w(WED, 10, 12))
    with pytest.raises(ConflictError):
        await planning.submit_request(session, leader.telegram_id, band.id, 60, "personal", None, [w(WED, 10, 12)])


async def test_disabled_weekday_cannot_be_requested(session, make_user, make_band):
    leader, band = await _band(make_user, make_band, "music", "Rockers")
    sunday = (await session.execute(select(models.AvailableDay).where(models.AvailableDay.day_of_week == 6))).scalar_one()
    sunday.is_enabled = False
    await session.commit()
    with pytest.raises(ConflictError):
        await _submit(session, leader, band, 1, w(6, 10, 12))


async def test_weekly_limit_counts_pending_requests(session, make_user, make_band):
    leader, band = await _band(make_user, make_band, "music", "Rockers")
    await _submit(session, leader, band, 3, w(WED, 10, 14))
    with pytest.raises(ConflictError):
        await _submit(session, leader, band, 2, w(1, 10, 14))  # 3h + 2h > 4h limit


async def test_responsible_person_may_submit_and_delete(session, make_user, make_band):
    leader, band = await _band(make_user, make_band, "music", "Rockers")
    member = await make_user()
    await bands.join_band(session, member.telegram_id, band.invite_code, "бас")
    detail = await bands.get_band_detail(session, band.id)
    member_id = next(m.id for m in detail.members if m.user_id == member.id)
    await bands.update_band_responsible(session, band.id, leader.telegram_id, member_id)

    created = await _submit(session, member, band, 1, w(WED, 10, 12))
    assert [r["id"] for r in await planning.list_requests(session, member.telegram_id)] == [created["id"]]
    await planning.delete_request(session, member.telegram_id, created["id"])
    assert await planning.list_requests(session, member.telegram_id) == []


async def test_stranger_cannot_see_or_delete_requests(session, make_user, make_band):
    leader, band = await _band(make_user, make_band, "music", "Rockers")
    stranger = await make_user()
    created = await _submit(session, leader, band, 1, w(WED, 10, 12))
    assert await planning.list_requests(session, stranger.telegram_id) == []
    with pytest.raises(ForbiddenError):
        await planning.delete_request(session, stranger.telegram_id, created["id"])


async def test_manual_booking_of_next_week_opens_only_after_publication(session, make_user, make_band):
    admin = await make_user(role="admin")
    leader, band = await _band(make_user, make_band, "music", "Rockers")
    # Monday 10-19 is inside the 7-day horizon but belongs to the unpublished next week.
    assert await bookings.list_available_slots(session, NEXT_MONDAY, 60, band.id) == []
    with pytest.raises(ConflictError):
        await bookings.create_booking(session, leader.telegram_id, band.id, NEXT_MONDAY, time(15), time(16), "self", "x")
    # the current week is unaffected
    assert await bookings.list_available_slots(session, date(2026, 10, 17), 60, band.id)

    await planning.run_weekly_allocation(session, admin.telegram_id)

    assert await bookings.list_available_slots(session, NEXT_MONDAY, 60, band.id)
    booking = await bookings.create_booking(session, leader.telegram_id, band.id, NEXT_MONDAY, time(15), time(16), "self", "x")
    assert booking.booking_date == NEXT_MONDAY


async def test_admins_can_still_occupy_an_unpublished_week(session, make_user):
    admin = await make_user(role="admin")
    booking = await bookings.create_staff_booking(session, admin.telegram_id, NEXT_MONDAY, time(15), time(16), "Закрыто")
    assert booking.source == "staff"


async def test_requests_close_at_the_sunday_deadline(session, make_user, make_band, freeze_now):
    leader, band = await _band(make_user, make_band, "music", "Rockers")
    freeze_now(datetime(2026, 10, 18, 17, 59))  # Sunday just before 18:00
    await _submit(session, leader, band, 1, w(WED, 10, 12))
    freeze_now(datetime(2026, 10, 18, 18, 0))
    with pytest.raises(ConflictError):
        await _submit(session, leader, band, 1, w(1, 10, 12))
    # on Monday the next round of requests (for the week after) opens
    freeze_now(datetime(2026, 10, 19, 9, 0))
    created = await _submit(session, leader, band, 1, w(1, 10, 12))
    assert created["week_start"] == "2026-10-26"


async def test_catch_up_runs_missed_allocation_once(session, make_user, make_band, freeze_now):
    leader, band = await _band(make_user, make_band, "music", "Rockers")
    await _submit(session, leader, band, 1, w(WED, 14, 16))
    freeze_now(datetime(2026, 10, 18, 20, 0))  # server was down at 18:00 on Sunday

    assert await planning.catch_up_allocations(session) == [NEXT_MONDAY]
    assert await planning.catch_up_allocations(session) == []
    assert len((await session.execute(select(models.Booking))).scalars().all()) == 1


async def test_catch_up_does_nothing_before_the_deadline(session, make_user, make_band):
    leader, band = await _band(make_user, make_band, "music", "Rockers")
    await _submit(session, leader, band, 1, w(WED, 14, 16))
    assert await planning.catch_up_allocations(session) == []
    assert await session.get(models.SchedulingWeek, NEXT_MONDAY) is None


async def test_overview_reports_counts_and_deadline(session, make_user, make_band):
    leader, band = await _band(make_user, make_band, "music", "Rockers")
    await _submit(session, leader, band, 1, w(WED, 14, 16))
    overview = await planning.get_week_overview(session)
    assert overview["week_start"] == "2026-10-19" and overview["is_open"] is True
    assert overview["deadline"] == "2026-10-18T18:00:00"
    assert overview["requests"]["pending"] == 1
