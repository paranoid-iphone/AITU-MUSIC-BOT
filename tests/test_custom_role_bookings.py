from datetime import time, timedelta

import pytest
from sqlalchemy import select

from app.core.timezone import now_local
from app.db import models
from app.services import bookings, planning, schedule_history
from app.services.errors import ConflictError, ForbiddenError


def _day(days: int = 1):
    return (now_local() + timedelta(days=days)).date()


async def _group_id(session, slug: str) -> int:
    return (await session.execute(select(models.Group.id).where(models.Group.slug == slug))).scalar_one()


async def _book(session, user, band, day, start_h, end_h, song="Song"):
    return await bookings.create_booking(session, user.telegram_id, band.id, day, time(start_h), time(end_h), "self", song)


async def test_bookings_manage_club_wide_edits_any_booking(session, make_user, make_band, role_holder):
    music_owner, dance_owner = await make_user(), await make_user()
    music_band = await make_band(music_owner, "Музыка", await _group_id(session, "music"))
    dance_band = await make_band(dance_owner, "Танцы", await _group_id(session, "dance"))
    manager = await role_holder("Брони", ["bookings.manage"])
    music_booking = await _book(session, music_owner, music_band, _day(), 10, 11)
    dance_booking = await _book(session, dance_owner, dance_band, _day(), 12, 13)
    personal = await bookings.create_booking(session, dance_owner.telegram_id, None, _day(), time(15), time(16), "personal", "соло")

    for booking in (music_booking, dance_booking, personal):
        updated = await bookings.update_booking(session, manager.telegram_id, booking.id, song_title="правка")
        assert updated.song_title == "правка"


async def test_bookings_manage_scoped_to_a_group_edits_only_that_group(session, make_user, make_band, role_holder):
    music_owner, dance_owner = await make_user(), await make_user()
    music_band = await make_band(music_owner, "Музыка", await _group_id(session, "music"))
    dance_band = await make_band(dance_owner, "Танцы", await _group_id(session, "dance"))
    music_booking = await _book(session, music_owner, music_band, _day(), 10, 11)
    dance_booking = await _book(session, dance_owner, dance_band, _day(), 12, 13)
    personal = await bookings.create_booking(session, dance_owner.telegram_id, None, _day(), time(15), time(16), "personal", "соло")
    manager = await role_holder("Брони Денс", ["bookings.manage"], "dance")

    assert (await bookings.update_booking(session, manager.telegram_id, dance_booking.id, song_title="ок")).song_title == "ок"
    for booking in (music_booking, personal):
        with pytest.raises(ForbiddenError, match="You cannot manage this booking"):
            await bookings.update_booking(session, manager.telegram_id, booking.id, song_title="нет")
    with pytest.raises(ForbiddenError):
        await bookings.delete_booking(session, manager.telegram_id, music_booking.id)
    assert (await bookings.delete_booking(session, manager.telegram_id, dance_booking.id)) is not None


async def test_priority_permission_takes_the_room(session, make_user, make_band, role_holder):
    owner = await make_user()
    band = await make_band(owner, "Rockers")
    await _book(session, owner, band, _day(), 14, 15)
    holder = await role_holder("Приоритет", ["bookings.priority"])
    outsider = await role_holder("Только брони", ["bookings.manage"])

    rows = await bookings.preview_priority_booking(session, holder.telegram_id, _day(), time(14, 30), time(15, 30))
    result = await bookings.create_priority_booking(session, holder.telegram_id, _day(), time(14, 30), time(15, 30), "Ректор")

    assert len(rows) == 1 and len(result["cancelled"]) == 1
    with pytest.raises(ForbiddenError, match="Only DSVR or admins can take over time"):
        await bookings.create_priority_booking(session, outsider.telegram_id, _day(), time(18), time(19), "x")
    with pytest.raises(ForbiddenError):
        await bookings.preview_priority_booking(session, outsider.telegram_id, _day(), time(18), time(19))


async def test_priority_permission_bypasses_the_weekly_limit_when_editing(session, make_user, make_band, role_holder):
    owner = await make_user()
    band = await make_band(owner, "Rockers")
    first = await _book(session, owner, band, _day(), 10, 11)
    for start in (12, 14, 16):  # four hours in all: the weekly limit of the test club
        await _book(session, owner, band, _day(), start, start + 1)
    privileged = await role_holder("Приоритет", ["bookings.priority", "bookings.manage"])
    plain_manager = await role_holder("Брони", ["bookings.manage"])

    with pytest.raises(ConflictError, match="Weekly booking limit exceeded"):
        await bookings.update_booking(session, plain_manager.telegram_id, first.id, end_time=time(12))
    longer = await bookings.update_booking(session, privileged.telegram_id, first.id, end_time=time(12))

    assert longer.end_time == time(12)


async def test_staff_booking_needs_the_priority_permission(session, make_user, role_holder):
    holder = await role_holder("Приоритет", ["bookings.priority"])
    outsider = await role_holder("Только брони", ["bookings.manage"])

    booking = await bookings.create_staff_booking(session, holder.telegram_id, _day(), time(10), time(11), "Совет")

    assert booking.purpose == "staff"
    with pytest.raises(ForbiddenError):
        await bookings.create_staff_booking(session, outsider.telegram_id, _day(), time(12), time(13), "Совет")


async def test_schedule_history_scope(session, make_user, make_band, role_holder):
    music_owner, dance_owner = await make_user(), await make_user()
    music_band = await make_band(music_owner, "Музыка", await _group_id(session, "music"))
    dance_band = await make_band(dance_owner, "Танцы", await _group_id(session, "dance"))
    await _book(session, music_owner, music_band, _day(), 10, 11)
    await _book(session, dance_owner, dance_band, _day(), 12, 13)
    club_wide = await role_holder("История", ["schedule.history"])
    dance_only = await role_holder("История Денс", ["schedule.history"], "dance")
    nobody = await make_user()

    everything = await schedule_history.list_changes(session, club_wide.telegram_id)
    scoped = await schedule_history.list_changes(session, dance_only.telegram_id)

    assert {row["band_name"] for row in everything} == {"Музыка", "Танцы"}
    assert {row["band_name"] for row in scoped} == {"Танцы"}
    with pytest.raises(ForbiddenError, match="You cannot view this history"):
        await schedule_history.list_changes(session, nobody.telegram_id)
    # a member of a band may still read that band's own history
    own = await schedule_history.list_changes(session, music_owner.telegram_id, band_id=music_band.id)
    assert {row["band_name"] for row in own} == {"Музыка"}


async def test_settings_manage_runs_the_allocation(session, make_user, role_holder):
    holder = await role_holder("Настройки", ["settings.manage"])
    outsider = await role_holder("Только брони", ["bookings.manage"])

    result = await planning.run_weekly_allocation(session, holder.telegram_id, dry_run=True)

    assert isinstance(result, dict)
    with pytest.raises(ForbiddenError, match="Admin access required"):
        await planning.run_weekly_allocation(session, outsider.telegram_id, dry_run=True)
